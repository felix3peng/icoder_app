# import libraries and helper files
def warn(*args, **kwargs):
    pass
import warnings
warnings.warn = warn
import time
import os
import base64
import sys
import re
import pickle
import socket
import openai
openai.api_key = os.getenv('OPENAI_API_KEY')
import matplotlib
matplotlib.use('Agg')
import numpy as np
import matplotlib.pyplot as plt
import pandas as pd
pd.set_option('display.max_columns', None)
pd.set_option('display.expand_frame_repr', False)
pd.set_option('display.max_rows', None)
pd.set_option('display.width', None)
from flask import Flask, Blueprint, flash, g, redirect, render_template
from flask import request, session, url_for, jsonify
from flask_sqlalchemy import SQLAlchemy
from io import StringIO, BytesIO
from datetime import datetime
from PIL import Image
from openai.embeddings_utils import get_embeddings

from resources import s00, cc_dict, cm_dict

# global declarations
global numtables, numplots
global codex_context, error_msg
global use_embeddings
use_embeddings = False
error_msg = 'Sorry, something went wrong. Please check the code and edit as needed.'

# string containing all commands and code to be fed to codex API
codex_context = ''
codex_context += '# import standard libraries\n'
codex_context += s00 + '\n'

'''
EMBEDDINGS
'''
# function to test whether cache file exists, evaluate whether it's up to date, and generate/re-generate if necessary
def test_cache():
    cache_path = 'embeddings_cache.pkl'
    # check local directory first (in case running from /app)
    print('\nChecking for cache file...')
    try:
        embedding_cache = pd.read_pickle(cache_path)
        print('Successfully located an embeddings cache file, reading...')
        if len(embedding_cache) != len(cm_dict):
            print('Cache file is outdated! Re-calculating embeddings...')
            # if cache doesn't have the right number of embeddings, re-run
            embedding_cache = get_embeddings(list(cm_dict.keys()),
                                            engine="text-similarity-davinci-001")
            with open(cache_path, "wb") as embedding_cache_file:
                pickle.dump(embedding_cache, embedding_cache_file)
                print('Successfully generated embeddings and dumped to cache file\n')
        else:
            print('Successfully loaded cached embeddings\n')
    except FileNotFoundError:
        # check if cache file exists in /app/ (in case running from parent directory)
        try:
            embedding_cache = pd.read_pickle(os.path.join('app', cache_path))
            print('Successfully located an embeddings cache file, reading...')
            if len(embedding_cache) != len(cm_dict):
                print('Cache file is outdated! Re-calculating embeddings...')
                # if cache doesn't have the right number of embeddings, re-run
                embedding_cache = get_embeddings(list(cm_dict.keys()),
                                                engine="text-similarity-davinci-001")
                with open(cache_path, "wb") as embedding_cache_file:
                    pickle.dump(embedding_cache, embedding_cache_file)
                    print('Successfully generated embeddings and dumped to cache file\n')
            else:
                print('Successfully loaded cached embeddings\n')
        # if cache file still not found, generate new cache file
        except FileNotFoundError:
            print('Did not find existing embeddings cache file. Creating embeddings and new cache...')
            embedding_cache = get_embeddings(list(cm_dict.keys()),
                                            engine="text-similarity-davinci-001")
            with open(cache_path, "wb") as embedding_cache_file:
                pickle.dump(embedding_cache, embedding_cache_file)
                print('Successfully generated embeddings and dumped to cache file\n')
    return embedding_cache

'''
HELPER FUNCTIONS
'''
# get computer name
global user_id
user_id = socket.gethostname()

# store normal stdout in variable for reference
old_stdout = sys.stdout

# initialize dictionary for storing variables generated by code
ldict = {}
numtables = 0
numplots = 0


# helper function for running raw code (in case of user edit or codex code)
exec(s00, ldict)
def runcode_raw(code):
    global numtables, numplots, error_msg
    # turn off plotting and run function, try to grab fig and save in buffer
    tldict = ldict.copy()
    plt.ioff()
    try:
        exec(code, tldict)
    except KeyError:
        pass
    except:
        print(error_msg)
    fig = plt.gcf()
    buf = BytesIO()
    fig.savefig(buf, format="png")
    plt.close()
    p = Image.open(buf)
    x = np.array(p.getdata(), dtype=np.uint8).reshape(p.size[1], p.size[0], -1)
    # if min and max colors are the same, it wasn't a plot - re-run as string
    if np.min(x) == np.max(x):
        new_stdout = StringIO()
        sys.stdout = new_stdout
        try:
            exec(code, ldict)
        except KeyError:
            pass
        except:
            print(error_msg)
        output = new_stdout.getvalue()
        sys.stdout = old_stdout

        # further parsing to determine if plain string or dataframe
        if bool(re.search('Index', output)):
            outputtype = 'string'
        elif bool(re.search(r'[\s]{3,}', output)):
            outputtype = 'dataframe'
            headers = re.split('\s+', output.partition('\n')[0])[1:]
            temp_df = pd.read_csv(StringIO(output.split('\n', 1)[1]), delimiter=r"\s{2,}", names=headers)
            temp_df
            if '[' in str(temp_df.index[-1]):
                temp_df.drop(temp_df.tail(1).index, inplace=True)
            output = temp_df.to_html(classes='table', table_id='table'+str(numtables), max_cols=500)
            numtables += 1
        else:
            outputtype = 'string'
        return [outputtype, output]
    # if it was a plot, then output as HTML image from buffer
    else:
        data = base64.b64encode(buf.getbuffer()).decode("ascii")
        output = "<img id='image{0}' src='data:image/png;base64,{1}'/>".format(numplots, data)
        outputtype = 'image'
        numplots += 1
        ldict.update(tldict)
        return [outputtype, output]


# log results to log db
def log_commands(outputs):
    # unpack outputs into variables
    _, cmd, code, _ = outputs
    feedback = 'none'
    dt = str(datetime.now())
    record = Log(dt, cmd, code, feedback)
    db.session.add(record)
    db.session.commit()
    return record.id


# log results to code edit db
def log_edit(edit):
    dt = str(datetime.now())
    command, orig_code, edited_code, orig_ref = edit
    record = Code_Edits(dt, command, orig_code, edited_code, orig_ref)
    db.session.add(record)
    db.session.commit()
    return record.id


# retrieve entry from log db using id
def get_log(id):
    record = Log.query.filter_by(id=id).first()
    cmd = record.command
    codeblock = record.codeblock
    return cmd, codeblock


# submit codex API call
def codex_call(prompt):
    start = time.time()
    response = openai.Completion.create(
        model="code-davinci-002",
        prompt=codex_context,
        temperature=0.01,
        max_tokens=4000,
        frequency_penalty=1,
        presence_penalty=1,
        stop=["#", "'''", '"""']
        )
    end = time.time()
    elapsed = end - start
    return response['choices'][0]['text'], elapsed


'''
FLASK APPLICATION CODE & ROUTES
'''
# set up flask application
app = Flask(__name__)
app.config.update(
    TESTING=True,
    SECRET_KEY='its-a-secret'
)

# set up database connection for log
db_name = 'log.db'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + db_name
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = True
db = SQLAlchemy(app)

# check for embedding cache file and load if exists, generate otherwise
# only need to run this if we are utilizing embeddings - disabled for openai-testing
if use_embeddings == True:
    embedding_cache = test_cache()

# create a class for the log table in db
class Log(db.Model):
    __tablename__ = 'log'
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.String(100))
    user = db.Column(db.String(100))
    command = db.Column(db.String(1000))
    codeblock = db.Column(db.String(1000))
    feedback = db.Column(db.String(1000))
    edit_ref = db.Column(db.Integer)

    def __init__(self, timestamp, command, codeblock, feedback):
        self.timestamp = timestamp
        self.user = user_id
        self.command = command
        self.codeblock = codeblock
        self.feedback = feedback
        self.edit_ref = None

# create a class for the code_edits table in db
class Code_Edits(db.Model):
    __tablename__ = 'code_edits'
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.String(100))
    command = db.Column(db.String(1000))
    orig_code = db.Column(db.String(1000))
    edited_code = db.Column(db.String(1000))
    orig_ref = db.Column(db.Integer)

    def __init__(self, timestamp, command, orig_code, edited_code, orig_ref):
        self.timestamp = timestamp
        self.command = command
        self.orig_code = orig_code
        self.edited_code = edited_code
        self.orig_ref = orig_ref


# base route to display main html body
@app.route('/', methods=["GET", "POST"])
def home():
    try:
        db.session.test_connection()
        pass
    except:
        flash('database connection failed')
    return render_template('icoder.html')


# create a route for login via SSO thru ZS credentials
@app.route('/login')
def login():
    pass


# create a route for callback from SSO
@app.route('/callback')
def callback():
    pass


# create a function to read form inputs and process a set of outputs in json
@app.route('/process')
def process():
    global codex_context
    print('Received command!')
    command = request.args.get('command').strip()
    # wrap multiline commands in quotes for block comments
    if '\n' in command:
        command = "'''\n" + command + "\n'''"
    # prepend single line commands with # for line comments
    else:
        command = '# ' + command
    print('Received command:')
    print(command)

    # check length of codex_context and trim if necessary
    # get positions of each command within the string, clear all but the most recent
    if len(codex_context) > 2000:
        print('Codex prompt is getting too long! Trimming...')
        command_positions = [(m.start(), m.end()) for m in re.finditer('#.+', codex_context)]
        block_key_positions = [(m.start(), m.end()) for m in re.finditer("'''.+'''", codex_context)]
        if command_positions[-1][0] > block_key_positions[-1][0]:
            codex_context = codex_context[command_positions[-1][0]:]
        else:
            codex_context = codex_context[block_key_positions[-1][0]:]
    codex_context += '\n' + command + '\n'

    # call openai api using code-davinci-002 to generate code from the command
    print('Calling codex API...')
    response, elapsed = codex_call(codex_context)
    print('Received response from codex API in {0:.2f} seconds.'.format(elapsed))
    codeblock = response.strip()
    print('Received code:\n')
    print(codeblock)

    # the response may be empty - we'll try one more time if so, but give up otherwise.
    if codeblock == '':
        print('No code generated! Adding a newline and trying again...')
        print('Calling codex API...')
        codex_context += '\n'
        response, elapsed = codex_call(codex_context)
        print('Received response from codex API in {0:.2f} seconds.'.format(elapsed))
        codeblock = response.strip()
        print('Received code:\n')
        print(codeblock)

        # if the returned code is still empty, remove the newline that was added and terminate
        if codeblock == '':
            print('No code generated!')
            codex_context = codex_context[:-1]
            outputs = ['string', command, '', 'No code generated!']
            
            # log results to database
            print('Logging results to database...')
            newest_id = log_commands(outputs)
            
            # update codex prompt log file
            print('Updating codex prompt...')
            with open(codex_filename, 'w') as f:
                f.write(codex_context)
            outputs.append(newest_id)

            # pass results back to client
            return jsonify(outputs=outputs)

    # if the last line is a declaration, wrap it in a print statement
    # fails if the codeblock is empty, wrap in try-except to avoid erroring out
    try:
        lastline = codeblock.splitlines()[-1]
        if ('=' not in lastline) and ('return' not in lastline) and ('print' not in lastline) and ('.fit' not in lastline) and ('plt' not in lastline):
            lastline_print = 'print(' + lastline + ')'
            codeblock = codeblock.replace(lastline, lastline_print)
            print('Caught last line as a declaration, wrapping in print statement...')
            print('Revised code:\n')
            print(codeblock)
    except IndexError:
        pass

    # strip leading and trailing whitespaces if included
    codex_context += codeblock + '\n'
    [outputtype, output] = runcode_raw(codeblock)
    outputs = [outputtype, command, codeblock, output]

    # write updated codex_context to file
    print('Updating codex prompt...')
    with open(codex_filename, 'w') as f:
        f.write(codex_context)
    
    # commit results to db and get id of corresponding entry
    print('Logging results to database...')
    newest_id = log_commands(outputs)

    # append id to outputs
    outputs.append(newest_id)

    return jsonify(outputs=outputs)


# create a function to clear local dictionary and reset codex_context when refresh is pressed
@app.route('/clear')
def clear():
    global codex_context, ldict
    codex_context = ''
    codex_context += '# import standard libraries\n'
    codex_context += s00
    ldict = {}
    exec(codex_context, ldict)
    return jsonify(outputs=[])


# create a function to process positive feedback
@app.route('/positive_feedback')
def positive_feedback():
    id = request.args.get('db_id')
    record = Log.query.filter_by(id=id).first()
    # update feedback; none if already positive, positive otherwise
    if record.feedback == 'positive':
        record.feedback = 'none'
        print('Canceled positive feedback on entry', id)
    else:
        record.feedback = 'positive'
        print('Positive feedback on entry', id)
    db.session.commit()
    return jsonify(id=id)


# create a function to process negative feedback
@app.route('/negative_feedback')
def negative_feedback():
    id = request.args.get('db_id')
    record = Log.query.filter_by(id=id).first()
    # update feedback; none if already negative, negative otherwise
    if record.feedback == 'negative':
        record.feedback = 'none'
        print('Canceled negative feedback on entry', id)
    else:
        record.feedback = 'negative'
        print('Negative feedback on entry', id)
    db.session.commit()
    return jsonify(id=id)


# create a function to process code edits
@app.route('/edit')
def edit():
    global codex_context
    print('Received edit')

    # edit database entry
    record_id = request.args.get('ref')
    newcmd = request.args.get('new_cmd')
    newcode = request.args.get('new_code')
    command, oldcode = get_log(record_id)
    [outputtype, output] = runcode_raw(newcode)
    outputs = [outputtype, output]
    edit = [command, oldcode, newcode, record_id]
    edit_record_id = log_edit(edit)
    orig_record = Log.query.filter_by(id=record_id).first()
    orig_record.edit_ref = edit_record_id
    db.session.commit()

    # edit codex script
    codex_context = codex_context.replace(oldcode, newcode)
    codex_context = codex_context.replace(command, newcmd)
    print('Successfully processed and recorded edit')
    return jsonify(outputs=outputs) 


# create a function to delete record from db
@app.route('/delete_record')
def delete_record():
    global codex_context
    id = request.args.get('db_id')
    print('Received delete request for record', id)
    
    # remove corresponding record from database
    record = Log.query.filter_by(id=id).first()
    db.session.delete(record)
    db.session.commit()

    # remove corresponding entry from codex prompter
    command = '# ' + record.command.strip().replace('\n', '\n# ') + '\n'
    codeblock = record.codeblock + '\n'
    codex_context = codex_context.replace(command, '')
    codex_context = codex_context.replace(codeblock, '')

    # write new codex prompt to file
    with open(codex_filename, 'w') as f:
        f.write(codex_context.strip())
    print('Successfully deleted record', id)
    return jsonify(id=id)

# create filename for storing codex prompt
codex_filename = 'codex_script_' + re.sub('\.[0-9]+', '', str(datetime.now()).replace(' ', '_').replace(':', '_')) + '.txt'
# check if working directory is currently in app folder and set up filename accordingly
if os.path.basename(os.getcwd()) == 'app':
    codex_filename = os.path.join('codex_logs', codex_filename)
else:
    codex_filename = os.path.join('app', 'codex_logs', codex_filename)

# start flask app
if __name__ == '__main__':
    app.run(host=socket.gethostbyname(user_id), port=5000, debug=True)

    # create file and write initial codex prompt
    with open(codex_filename, 'w+') as f:
        f.write(codex_context)

