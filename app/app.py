# import libraries and helper files
def warn(*args, **kwargs):
    pass
import warnings
warnings.warn = warn
import os
import numpy as np
import pandas as pd
pd.set_option('display.max_columns', 500)
pd.set_option('display.expand_frame_repr', False)
from flask import Flask, Blueprint, flash, g, redirect, render_template
from flask import request, session, url_for, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.sql import text
import sqlite3
import openai
import inspect
from itertools import groupby
from subprocess import Popen, PIPE
from io import StringIO, BytesIO
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.figure import Figure
import base64
import sys
import re
from datetime import datetime
from PIL import Image
import openai
openai.api_key = os.getenv('OPENAI_API_KEY')
from openai.embeddings_utils import get_embeddings, distances_from_embeddings
from openai.embeddings_utils import get_embedding, cosine_similarity
import pickle
import shap
import socket
from resources import cc_dict, cm_dict

# global declarations
global numtables, numplots
global codex_context

codex_context = ''

'''
COMMAND-CODE DICTIONARY
'''

'''
EMBEDDINGS
'''
cache_path = 'embeddings_cache.pkl'
try:
    embedding_cache = pd.read_pickle(cache_path)
    print('cache file located, reading...')
    if len(embedding_cache) != len(cm_dict):
        print('outdated cache file, re-calculating embeddings...')
        # if cache doesn't have the right number of embeddings, re-run
        embedding_cache = get_embeddings(list(cm_dict.keys()),
                                         engine="text-similarity-davinci-001")
        with open(cache_path, "wb") as embedding_cache_file:
            pickle.dump(embedding_cache, embedding_cache_file)
            print('successfully dumped embeddings to cache')
    else:
        print('successfully loaded cached embeddings')
except FileNotFoundError:
    print('cache file not found, creating new cache')
    embedding_cache = get_embeddings(list(cm_dict.keys()),
                                     engine="text-similarity-davinci-001")
    with open(cache_path, "wb") as embedding_cache_file:
        pickle.dump(embedding_cache, embedding_cache_file)
        print('successfully dumped embeddings to cache')


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


# helper function for running code stored in dictionary
# passing on KeyErrors when re-running due to column drop errors
def runcode(text, args=None):
    global numtables, numplots
    # turn off plotting and run function, try to grab fig and save in buffer
    tldict = ldict.copy()
    plt.ioff()
    if args is None:
        try:
            exec(cc_dict[text], tldict)
        except:
            print('something went wrong. ensure target & train-test split set')
    elif len(args) == 1:
        try:
            exec(cc_dict[text].format(args[0]), tldict)
        except:
            print('something went wrong. ensure target & train-test split set')
    else:
        try:
            exec(cc_dict[text].format(*args), tldict)
        except:
            print('something went wrong. ensure target & train-test split set')
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
        if args is None:
            try:
                exec(cc_dict[text], ldict)
            except:
                print('something went wrong. ensure target & train-test split set')
        elif len(args) == 1:
            try:
                exec(cc_dict[text].format(args[0]), ldict)
            except KeyError:
                pass
            except:
                print('something went wrong. ensure target & train-test split set')
        else:
            try:
                exec(cc_dict[text].format(*args), ldict)
            except KeyError:
                pass
            except:
                print('something went wrong. ensure target & train-test split set')
        output = new_stdout.getvalue()
        sys.stdout = old_stdout
        # further parsing to determine if plain string or dataframe
        if bool(re.search(r'[\s]{3,}', output)):
            outputtype = 'dataframe'
            temp_df = pd.read_csv(StringIO(output), delim_whitespace=True)
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


# log results to db
def log_commands(outputs):
    # unpack outputs into variables
    _, cmd, code, _ = outputs
    feedback = 'none'
    dt = str(datetime.now())
    record = Log(dt, cmd, code, feedback)
    db.session.add(record)
    db.session.commit()
    return record.id


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

# create a class for the table in db
class Log(db.Model):
    __tablename__ = 'log'
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.String(100))
    user = db.Column(db.String(100))
    command = db.Column(db.String(1000))
    codeblock = db.Column(db.String(1000))
    feedback = db.Column(db.String(1000))

    def __init__(self, timestamp, command, codeblock, feedback):
        self.timestamp = timestamp
        self.user = user_id
        self.command = command
        self.codeblock = codeblock
        self.feedback = feedback

# base route to display main html body
@app.route('/', methods=["GET", "POST"])
def home():
    try:
        db.session.test_connection()
        pass
    except:
        flash('database connection failed')
    return render_template('icoder.html')


# create a function to read form inputs and process a set of outputs in json
@app.route('/process')
def process():
    command = request.args.get('command')
    extra_args = []

    # check for any feature names
    feat_params = [a.strip() for a in command.split() if a.isupper()]
    # strip commas and quotes from feature names
    feat_params = [a.replace(',', '') for a in feat_params]
    feat_params = [a.replace('"', '') for a in feat_params]
    feat_params = [a.replace("'", '') for a in feat_params]

    # set up list of all caps names that should be ignored
    restricted_allcaps = ['X', 'Y', 'TARGET', 'TRAIN', 'TEST', 'LOG', 'HCP', 'DEA', 'MAE', 'RMSE', 'TRx', 'NBRx']
    # check if any elements of restricted_allcaps are in feat_params and remove them
    for feat in feat_params:
        if feat in restricted_allcaps:
            feat_params.remove(feat)
    
    # if there are any feature names, then add them to extra_args
    if len(feat_params) > 0:
        extra_args.extend(feat_params)

    # turn to lowercase for uniformity
    lcommand = command.lower()

    # parse command for any numbers, ignoring numbers adjacent to letters (e.g. R2)
    num_params = re.findall(r'[\s-]*(\d+)[\s-]*', lcommand)
    # parse train-test ratio
    if len(num_params) > 1:
        nums = [float(n) for n in num_params]
        nums = [n/100 for n in nums if n > 1.0]
        nums = [str(round(n, 2)) for n in nums]
        extra_args.extend(nums)
    # parse single-number parameters (e.g. number of trees)
    elif len(num_params) == 1:
        extra_args.extend(num_params)

    # check if command is in the dictionary keys; if not, match via embedding
    cmd_match = True
    if lcommand not in list(cm_dict.keys()):
        cmd_embed = get_embedding(lcommand)
        sims = [cosine_similarity(cmd_embed, x) for x in embedding_cache]
        ind = np.argmax(sims)
        # for debugging; print out command matching schema
        print('\n\nEntered: ', command)
        print('Best match similarity: ', np.max(sims))
        print('Best match command: ', list(cm_dict.keys())[ind])
        # set cmd_match flag to False if best similarity is 0.80 or less
        if np.max(sims) <= 0.80:
            cmd_match = False
            print('Best match rejected, calling Codex API...\n')
        else:
            cmd = list(cm_dict.keys())[ind]
            base_cmd = cm_dict[cmd]
            code = cc_dict[base_cmd]
            print('Best match accepted\n')
    else:
        cmd = command.lower()
        base_cmd = cm_dict[cmd]
        code = cc_dict[base_cmd]

    # supplement cmd with parameters (if applicable) and pass to runcode
    if cmd_match == True:
        argtuple = tuple(extra_args)
        if len(argtuple) == 1:
            codeblock = code.format(argtuple[0])
        else:
            codeblock = code.format(*argtuple)
        print(codeblock, '\n')
        if len(argtuple) > 0:
            [outputtype, output] = runcode(cmd, argtuple)
        else:
            [outputtype, output] = runcode(cmd)
        outputs = [outputtype, command, codeblock, output]
    elif cmd_match == False:
        # call OpenAI codex API to get codeblock
        
        outputs = ['string', command, '', 'No matching command found']
    
    # commit results to db and get id of corresponding entry
    newest_id = log_commands(outputs)
    # append id to outputs
    outputs.append(newest_id)

    return jsonify(outputs=outputs)

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

if __name__ == '__main__':
    app.run(debug=True)