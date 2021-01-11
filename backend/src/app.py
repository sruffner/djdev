import dash
import dash_bootstrap_components as dbc
import datajoint as dj
import dash_uploader as du
import os
from pathlib import Path

# bootstrap theme
# https://bootswatch.com/lux/
ext_ss = [dbc.themes.SPACELAB]

app = dash.Dash(__name__, external_stylesheets=ext_ss)

server = app.server
app.config.suppress_callback_exceptions = True

# configure Dash uploader to upload to staging directory in backend container
if 'DJDEV_ROOT_REPO' not in os.environ:
    raise RuntimeError('The environment variable DJDEV_ROOT_REPO is required.')
upload_dir = Path(os.environ['DJDEV_ROOT_REPO'], 'staging')
du.configure_upload(app, str(upload_dir))

dj.config['database.host'] = 'db'
dj.config['database.user'] = 'root'
dj.config['safemode'] = False
if 'MYSQL_ROOT_PASSWORD' not in os.environ:
    raise RuntimeError('The environment variable MYSQL_ROOT_PASSWORD is required.')
dj.config['database.password'] = os.environ['MYSQL_ROOT_PASSWORD']
