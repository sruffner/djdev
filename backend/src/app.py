import dash
import dash_bootstrap_components as dbc
import datajoint as dj

# bootstrap theme
# https://bootswatch.com/lux/
ext_ss = [dbc.themes.SPACELAB]

app = dash.Dash(__name__, external_stylesheets=ext_ss)

server = app.server
app.config.suppress_callback_exceptions = True

# TODO: The database user information needs to be 'secret'
dj.config['database.host'] = 'db'
dj.config['database.user'] = 'root'
dj.config['database.password'] = 'coltrane'
dj.config['safemode'] = False
