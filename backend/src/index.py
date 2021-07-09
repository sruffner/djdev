"""
index.py: The application entry point and "front page" for the Dash app implementing the Lisberger lab data portal.

This page defines the layout and callbacks for the portal's landing page. The portal is a multi-page web app, and the
landing page handles URL routing to the different pages. It also handles user login/logout via a button and dropdown
menu in the navigation bar along the top of the browser page.

User authentication/login is implemented using the Flask-Login library. It is ASSUMED that the Dash app is hosted
behind a reverse proxy that implements SSL so that all requests and responses are encrypted.

Note the "__main__"" entry point at the end of the file. The portal application is started in Python with the
command "python ./index.py". The Dash application instance is created in app.py.

@created: oct2020
@author: sruffner
"""
import dash
import dash_core_components as dcc
import dash_html_components as html
from dash.dependencies import Input, Output, State
import dash_bootstrap_components as dbc
import flask_login

from app import app, load_authorized_user
from database.manager import DataBaseManager
from pages import home, curate, commit_session, explore, neurons


def _serve_layout() -> html.Div:
    modal_login = dbc.Modal(
        [
            dbc.ModalHeader(f"Login to portal"),
            dbc.ModalBody(dbc.Form([
                dbc.FormGroup([
                    dbc.Label("Username", width=2),
                    dbc.Col(dbc.Input(type="text", id="login-username", placeholder="Enter username"), width=10)
                ], row=True),
                dbc.FormGroup([
                    dbc.Label("Password", width=2),
                    dbc.Col(dbc.Input(type="password", id="login-password", placeholder="Enter password"), width=10)
                ], row=True),
                dbc.FormGroup(
                    dbc.Alert("", id="login-alert", is_open=False)
                )
            ])),
            dbc.ModalFooter(
                dbc.Row([
                    dbc.Button("Login", id="login-submit-btn", color="primary", n_clicks=0),
                    dbc.Button("Cancel", id="login-cancel-btn", color="primary", n_clicks=0, className="ml-3")
                ])
            )
        ],
        id="login-modal", backdrop="static", size="lg", is_open=False
    )

    # If a user is logged in, the dropdown menu is rendered in the nav bar with items enabled/disabled depending on the
    # user's access level. If not, the Login button is rendered.
    login_btn_style = None
    drop_menu_style = dict(display='none')
    drop_menu_label = "Welcome"
    can_curate = False
    can_commit = False
    if flask_login.current_user.is_authenticated:
        portal_user = load_authorized_user(flask_login.current_user.get_id())
        if portal_user is not None:
            login_btn_style = dict(display='none')
            drop_menu_style = None
            drop_menu_label = f"Welcome, {portal_user.first_name()}"
            can_curate = portal_user.can_curate_database()
            can_commit = portal_user.can_commit_to_database()

    navbar = dbc.Navbar(
        [
            html.A(dbc.NavbarBrand("Lisberger Data Portal"), href="/home"),
            dbc.Row(
                [
                    dbc.Col(dbc.Button("Login", id='login-btn', color="info", style=login_btn_style, n_clicks=0),
                            width="auto"),
                    dbc.Col(
                        dbc.DropdownMenu(
                            children=[
                                dbc.DropdownMenuItem("What do you want to do?", header=True),
                                dbc.DropdownMenuItem("Explore the database", href="/explore"),
                                dbc.DropdownMenuItem("Curate lab information (access restricted)", id='link-curate',
                                                     href="/curate", disabled=not can_curate),
                                dbc.DropdownMenuItem("Commit experiment session (access restricted)", id='link-commit',
                                                     href="/commit_session", disabled=not can_commit),
                                dbc.DropdownMenuItem(divider=True),
                                dbc.DropdownMenuItem("Logout", id='logout-btn', n_clicks=0)
                            ],
                            id='nav-menu', right=True, label=drop_menu_label, color="info", style=drop_menu_style
                        ),
                        width="auto"
                    )
                ],
                no_gutters=True, className="ml-auto flex-nowrap mt-3 mt-md-0", align="center"
            )
        ],
        color="primary",
        dark=True,
    )

    # the div encapsulating page content is populuated each time the URL changes
    return html.Div([
        dcc.Location(id='url', refresh=False),
        dcc.Location(id='redirect', refresh=True),
        navbar,
        modal_login,
        html.Div(id='page-content')
    ])


# by implementing the front-page layout as a function, we can always check for a logged-in user to get the nav bar right
app.layout = _serve_layout


@app.callback([Output('page-content', 'children'), Output('redirect', 'pathname')],
              [Input('url', 'pathname')])
def display_page(pathname):
    redirect_url = dash.no_update
    if pathname == '/explore':
        layout = explore.layout
    elif pathname == '/neurons':
        layout = neurons.serve_layout()
    else:
        can_curate = can_commit = False
        if flask_login.current_user.is_authenticated:
            portal_user = load_authorized_user(flask_login.current_user.get_id())
            if portal_user:
                can_curate = portal_user.can_curate_database()
                can_commit = portal_user.can_commit_to_database()
        if pathname == '/curate':
            layout = curate.layout if can_curate else None
            redirect_url = dash.no_update if can_curate else '/home'
        elif pathname == '/commit_session':
            layout = commit_session.layout if can_commit else None
            redirect_url = dash.no_update if can_commit else '/home'
        else:
            layout = home.serve_layout(can_curate, can_commit)
    return layout, redirect_url


@app.callback(
    [Output('login-modal', 'is_open'), Output('login-username', 'value'), Output('login-password', 'value'),
     Output('nav-menu', 'label'), Output('nav-menu', 'style'), Output('login-btn', 'style'),
     Output('link-curate', 'disabled'), Output('link-commit', 'disabled'), Output('login-alert', 'children'),
     Output('login-alert', 'is_open'), Output('url', 'pathname')],
    [Input('login-btn', 'n_clicks'), Input('login-submit-btn', 'n_clicks'), Input('login-cancel-btn', 'n_clicks'),
     Input('logout-btn', 'n_clicks')],
    [State('login-username', 'value'), State('login-password', 'value')]
)
def login_callback(*args):
    ctx = dash.callback_context
    out = [dash.no_update] * 11
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if (ctx.triggered is not None) else ""

    if trigger_id == 'login-btn':
        out[0] = True
        out[1] = out[2] = ""
        out[9] = False
    elif trigger_id == 'login-submit-btn':
        username = args[4] if isinstance(args[4], str) else ""
        password = args[5] if isinstance(args[5], str) else ""
        error_msg = DataBaseManager().authenticate_portal_user(username, password)
        portal_user = None
        if error_msg is None:
            portal_user = load_authorized_user(username)
            if portal_user is None:
                error_msg = 'Failed to find user account record'
            else:
                flask_login.login_user(portal_user)
        if portal_user:  # successful login!
            out[0] = False
            out[1] = out[2] = ""
            out[3] = f"Welcome, {portal_user.first_name()}"
            out[4] = None
            out[5] = dict(display='none')
            out[6] = not portal_user.can_curate_database()
            out[7] = not portal_user.can_commit_to_database()
            out[8] = ""
            out[9] = False
            out[10] = '/home'
        else:
            out[8] = error_msg
            out[9] = True
    elif trigger_id == 'login-cancel-btn':
        out[0] = False
        out[1] = out[2] = ""
        out[9] = False
    elif trigger_id == 'logout-btn':
        flask_login.logout_user()
        out[3] = "Welcome"
        out[4] = dict(display='none')
        out[5] = None
        out[6] = True
        out[7] = True
        out[10] = '/home'
    return tuple(out)


if __name__ == '__main__':
    mgr = DataBaseManager()
    msg = mgr.on_startup()
    if msg:
        print(f"===> {msg}", flush=True)
    app.run_server(host='0.0.0.0', port='8050', debug=True)
