import dash
import dash_bootstrap_components as dbc
import datajoint as dj
import os

# bootstrap theme
# https://bootswatch.com/lux/
ext_ss = [dbc.themes.SPACELAB]

app = dash.Dash(__name__, external_stylesheets=ext_ss)

server = app.server
app.config.suppress_callback_exceptions = True

dj.config['database.host'] = 'db'
dj.config['database.user'] = 'root'
dj.config['safemode'] = False
if not 'MYSQL_ROOT_PASSWORD' in os.environ:
    raise RuntimeError('The environment variable MYSQL_ROOT_PASSWORD is required.')
dj.config['database.password'] = os.environ['MYSQL_ROOT_PASSWORD']
