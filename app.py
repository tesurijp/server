from flask import Flask, g, session, abort, redirect, url_for, render_template, request, escape, send_from_directory, jsonify
from werkzeug.utils import secure_filename
from functools import wraps
from sqlalchemy import desc
from flask_sqlalchemy import SQLAlchemy
import hashlib
import requests
import os
import shutil
import json
import base64
import datetime
import subprocess
import bcrypt

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(APP_ROOT, 'uploads')
ALLOWED_EXTENSIONS = set(['pdf'])

app = Flask(__name__)

# set the upload path
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# set the secret key.  keep this really secret:
app.secret_key = 'ff29b42f8d7d5cbefd272eab3eba6ec8'

app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://localhost/libreread_dev'
db = SQLAlchemy(app)

from models import User, Book

@app.before_request
def before_request():
    if 'email' in session:
        g.user = session['email']
    else:
        g.user = None

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if g.user is None:
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/')
def index():
    if 'email' in session:
        user = User.query.filter_by(email=session['email']).first()
        books = user.books.all()
        print books
        if len(books):
            new_books = Book.query.filter_by(user_id=user.id).order_by(desc(Book.created_on)).limit(5).all()
            print new_books
        return render_template('home.html', user = user, books = books, new_books=new_books)
    return render_template('landing.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        password = request.form['password']

        password_hash = bcrypt.hashpw(password, bcrypt.gensalt())

        user = User(name, email, password_hash)
        db.session.add(user)
        db.session.commit()
        session['email'] = email

        return redirect(url_for('index'))
    return '''
        <form action="" method="post">
            <p><input type=text name=name></p>
            <p><input type=text name=email></p>
            <p><input type=text name=password></p>
            <p><input type=submit value=sign up></p>
        </form>
    '''

@app.route('/signin', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']

        user = User.query.filter_by(email=email).first()

        if user is not None:
            if bcrypt.hashpw(password, user.password_hash) == user.password_hash:
                session['email'] = email
                return redirect(url_for('index'))
    return '''
        <form action="" method="post">
            <p><input type=text name=email></p>
            <p><input type=text name=password></p>
            <p><input type=submit value=Login></p>
        </form>
    '''

@app.route('/signout')
def logout():
    # remove the email from the session if it's there
    session.pop('email', None)
    return redirect(url_for('index'))

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1] in ALLOWED_EXTENSIONS

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'],
                               filename)

@app.route('/book-upload', methods=['GET', 'POST'])
def upload_file():
    if request.method == 'POST':
        for i in range(len(request.files)):
          file = request.files['file['+str(i)+']']
          if file.filename == '':
              print ('No selected file')
              return redirect(request.url)
          if file and allowed_file(file.filename):
              filename = secure_filename(file.filename)
              file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
              file.save(file_path)

              info = _pdfinfo(file_path)
              print (info)

              img_folder = 'images/' + '_'.join(info['Title'].split(' '))
              cover_path = os.path.join(app.config['UPLOAD_FOLDER'], img_folder)

              _gen_cover(file_path, cover_path)

              url = '/b/' + filename
              cover = '/b/cover/' + '_'.join(info['Title'].split(' ')) + '-001-000.png'
              print cover

              book = Book(title=info['Title'], author=info['Author'], url=url, cover=cover, pages=info['Pages'], current_page=0)

              user = User.query.filter_by(email=session['email']).first()
              user.books.append(book)
              db.session.add(user)
              db.session.add(book)
              db.session.commit()

              print '\n\n\n'
              print book.id
              # Feeding pdf content into ElasticSearch
              # Encode the pdf file and add it to the index
              pdf_data = _pdf_encode(file_path)

              # Set the payload in json
              book_info = json.dumps({
                'title': book.title,
                'author': book.author,
                'url': book.url,
                'cover': book.cover
              })

              # Send the request to ElasticSearch on localhost:9200
              r = requests.put('http://localhost:9200/lr_index/book_info/' + str(book.id), data=book_info)
              print r.text

              # Make directory for adding the pdf separated files
              directory = os.path.join(app.config['UPLOAD_FOLDER'], 'splitpdf')
              if not os.path.exists(directory):
                  os.makedirs(directory)

              _pdf_separate(directory, file_path)

              for i in range(1,int(book.pages)+1):
                  pdf_data = _pdf_encode(directory+'/'+str(i)+'.pdf')
                  book_detail = json.dumps({
                      'thedata': pdf_data,
                      'title': book.title,
                      'author': book.author,
                      'url': book.url,
                      'cover': book.cover,
                      'page': i,
                  })
                  # feed data in id = userid_bookid_pageno
                  r = requests.put('http://localhost:9200/lr_index/book_detail/' + str(user.id) + '_' + str(book.id) + '_' + str(i) + '?pipeline=attachment', data=book_detail)
                  print r.text

              # Remove the splitted pdfs as it is useless now
              shutil.rmtree(directory)

              print user.books

              print ('Book uploaded successfully!')
        return 'success'
    else:
        return redirect(url_for('index'))

def _pdf_separate(directory, file_path):
    subprocess.call('pdfseparate ' + file_path + ' ' + directory + '/%d.pdf', shell=True)

def _pdfinfo(infile):
    """
    Wraps command line utility pdfinfo to extract the PDF meta information.
    Returns metainfo in a dictionary.
    sudo apt-get install poppler-utils
    This function parses the text output that looks like this:
        Title:          PUBLIC MEETING AGENDA
        Author:         Customer Support
        Creator:        Microsoft Word 2010
        Producer:       Microsoft Word 2010
        CreationDate:   Thu Dec 20 14:44:56 2012
        ModDate:        Thu Dec 20 14:44:56 2012
        Tagged:         yes
        Pages:          2
        Encrypted:      no
        Page size:      612 x 792 pts (letter)
        File size:      104739 bytes
        Optimized:      no
        PDF version:    1.5
    """
    import os.path as osp
    import subprocess

    cmd = '/usr/bin/pdfinfo'
    # if not osp.exists(cmd):
    #     raise RuntimeError('System command not found: %s' % cmd)

    if not osp.exists(infile):
        raise RuntimeError('Provided input file not found: %s' % infile)

    def _extract(row):
        """Extracts the right hand value from a : delimited row"""
        return row.split(':', 1)[1].strip()

    output = {}

    labels = ['Title', 'Author', 'Creator', 'Producer', 'CreationDate',
              'ModDate', 'Tagged', 'Pages', 'Encrypted', 'Page size',
              'File size', 'Optimized', 'PDF version']

    cmd_output = subprocess.check_output(['pdfinfo', infile])
    for line in cmd_output.splitlines():
        for label in labels:
            if label in line:
                output[label] = _extract(line)

    return output

def _gen_cover(file_path, cover_path):
    print file_path
    print cover_path
    subprocess.call('pdfimages -p -png -f 1 -l 2 ' + file_path + ' ' + cover_path, shell=True)

def _pdf_encode(pdf_filename):
    return base64.b64encode(open(pdf_filename,"rb").read());

@app.route('/b/<filename>')
def send_book(filename):
    return send_from_directory('uploads', filename)

@app.route('/b/cover/<filename>')
def send_book_cover(filename):
    return send_from_directory('uploads/images', filename)

@app.route('/autocomplete')
def search_books():
    query = request.args.get('term')
    print query

    suggestions = []

    payload = json.dumps({
        '_source': ['title', 'author', 'url', 'cover'],
        'query': {
            'multi_match': {
                'query': query,
                'fields': ['title', 'author']
            }
        }
    })

    r = requests.get('http://localhost:9200/lr_index/book_info/_search', data=payload)
    data = json.loads(r.text)
    print (data)

    hits = data['hits']['hits']
    total = int(data['hits']['total'])

    metadata = []

    for hit in hits:
        title = hit['_source']['title']
        author = hit['_source']['author']
        url = hit['_source']['url']
        cover = hit['_source']['cover']

        metadata.append({
            'title': title, 'author': author, 'url': url, 'cover': cover
        })


    suggestions.append(metadata)

    payload = json.dumps({
        '_source': ['title', 'author', 'url', 'cover'],
        'query': {
            'match_phrase': {
                'attachment.content': query
            }
        },
        'highlight': {
            'fields': {
                'attachment.content': {
                    'fragment_size': 150,
                    'number_of_fragments': 3,
                    'no_match_size': 150
                }
            }
        }
    })

    r = requests.get('http://localhost:9200/lr_index/book_detail/_search', data=payload)
    data = json.loads(r.text)
    print (data)

    hits = data['hits']['hits']
    total = int(data['hits']['total'])

    content = []

    for hit in hits:
        title = hit['_source']['title']
        author = hit['_source']['author']
        url = hit['_source']['url']
        cover = hit['_source']['cover']
        data = hit['highlight']['attachment.content']
        if len(data):
            for i in data:
                content.append({
                    'title': title, 'author': author, 'url': url, 'cover': cover, 'data': i
                })

    suggestions.append(content)

    return jsonify(suggestions)