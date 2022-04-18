"""
index.py: The application entry point and "front page" for the Dash app implementing the Lisberger lab data portal.

This page defines the layout and callbacks for the portal's landing page. The portal is a multi-page web app, and the
landing page handles URL routing to the different pages. It also handles user login/logout via a button and dropdown
menu in the navigation bar along the top of the browser page.

User authentication/login is implemented using the Flask-Login library. It is ASSUMED that the Dash app is hosted
behind a reverse proxy that implements SSL so that all requests and responses are encrypted.

There are currently 3 different "access levels" for authenticated users; the access level determine what routes in this
"multi-page" app are accessible to the client:
    1) Anonymous user: "/explore".
    2) Authenticated "download" user: Same routes as (1), with the ability to download data sets. Also has access to
        "/user_profile" (by which the user can edit their own profile or change their password).
    3) Authenticated "commit" user: Same routes as (2), plus "/commit_session".
    4) Authenticated "admin" user: All routes.
Since a user could choose to access this app across multiple tabs in the browser, then later logout on any one of the
tabs, the other tabs could expose content to which the client should no longer have access. All restricted pages must
handle this scenario. There's also the possibility that the user could leave open the browser tab(s) and the user
session subsequently expires. For this reason, a Dash Interval component fires once every 10 seconds to check the
client's authentication status and redirect to "/explore" if the client is logged out or otherwise lacks the required
access level for the current page content.

Note the "__main__"" entry point at the end of the file. When the portal application is started in Python with the
command "python ./index.py", the application runs an the Flask development server. This mode of "deployment" is only for
test and development purposes when running in a Docker environment on a single development workstation. For production
deployment, multiple replicas of the Dash app are managed by a GUnicorn server. In this scenario, the Dash/Flask
application instance is obtained via get_app(), and GUnicorn is launched by the command "gunicorn --config path/to/cfg
index:get_app()". The Dash/Flask application instance is created and configured, along with other application
configuration, in app.py.

@created: oct2020
@author: sruffner
"""
from app import app, load_authorized_user

from urllib.parse import urlparse, urlunparse
from dash import callback, callback_context, no_update, Input, Output, State, dcc, html
import dash_bootstrap_components as dbc
import flask_login

from config.app_logging import get_application_logger
from database.user_ops import authenticate_portal_user, validate_username, validate_password
from pages import commit, explore, user_profile, download_history, manage

_LOGIN_MODAL_ID = "login-modal"
""" ID of the Login modal window component. """
_LOGIN_USERNAME_ID = "login-username"
""" ID of Login modal's form widget in which username is entered. """
_LOGIN_PASSWORD_ID = "login-password"
""" ID of Login modal's form widget in which user's password is entered. """
_LOGIN_ALERT_ID = "login-alert"
""" ID of Bootstrap Alert that displays error message in Login modal window when a login attempt fails. """
_LOGIN_SUBMIT_ID = "login-submit-btn"
""" ID of button to initiate login attempt on Login modal window. """
_LOGIN_CANCEL_ID = "login-cancel-btn"
""" ID of button to cancel login attempt on Login modal window. """
_LOGIN_ID = "login-btn"
""" ID of button on navigation bar that raises the Login modal window. """
_NAV_MENU_ID = "nav-menu"
""" ID of dropdown menu in navigation bar exposing parts of the portal accessible only to authenticated clients. """
_LINK_COMMIT_ID = "link-commit"
""" ID of link-style menu item in navigation bar's dropdown menu that links to the 'commit session' page. """
_LINK_DOWNLOADS_ID = "link-download-history"
""" ID of link-style menu item in navigation bar's dropdown menu that links to the 'download history' page. """
_LINK_ADMIN_ID = "link-admin"
""" ID of link-style menu item in navigation bar's dropdown menu that links to the 'portal administration' page. """
_LOGOUT_ID = "logout-btn"
""" ID of button-style menu item in navigation bar's dropdown menu that logs out the current user. """
_URL_ID = "url"
""" ID of Dash Location component representing the current browser location. """
_PAGE_CONTENT_ID = "page-content"
""" ID of the main Div (below the navigation bar) encapsulating the current page's content in this multi-page app. """
_AUTH_INTV_ID = "check-auth-intv"
""" ID of an Interval component firing once per minute to check if current user is authorized to access page. """

_PUBLIC_ENDPOINTS = ['/', '/explore']
""" Public endpoints in the portal web app that do not require authenticated access. """


def _serve_layout() -> html.Div:
    modal_login = dbc.Modal(
        [
            dbc.ModalHeader(f"Login to portal"),
            dbc.ModalBody(dbc.Form([
                dbc.Row([
                    dbc.Label("Username", width=2),
                    dbc.Col([
                        dbc.Input(type="text", id=_LOGIN_USERNAME_ID, placeholder="Enter username"),
                        dbc.FormFeedback(
                            "Username must be 3-20 lowercase letters or digits, starting with a lowercase letter",
                            type='invalid')
                    ], width=10)
                ], class_name='mb-2'),
                dbc.Row([
                    dbc.Label("Password", width=2),
                    dbc.Col([
                        dbc.Input(type="password", id=_LOGIN_PASSWORD_ID, placeholder="Enter password"),
                        dbc.FormFeedback(
                            "Password must be 8-32 chars with at least one digit and one uppercase letter",
                            type='invalid')
                    ], width=10)
                ], class_name='mb-2'),
                dbc.Row(
                    dbc.Col(dbc.Alert("", id=_LOGIN_ALERT_ID, is_open=False, duration=3000), width=12)
                )
            ])),
            dbc.ModalFooter(
                dbc.Row([
                    dbc.Col(dbc.Button("Login", id=_LOGIN_SUBMIT_ID, n_clicks=0, disabled=True), width='auto'),
                    dbc.Col(dbc.Button("Cancel", id=_LOGIN_CANCEL_ID, n_clicks=0), width='auto')
                ], justify='end')
            )
        ],
        id=_LOGIN_MODAL_ID, backdrop="static", size="lg", is_open=False
    )

    # If a user is logged in, the dropdown menu is rendered in the nav bar with items enabled/disabled depending on the
    # user's access level. If not, the Login button is rendered.
    login_btn_style = None
    drop_menu_style = dict(display='none')
    drop_menu_label = "Welcome"
    can_commit = is_admin = False
    if flask_login.current_user.is_authenticated:
        portal_user = load_authorized_user(flask_login.current_user.get_id())
        if portal_user is not None:
            login_btn_style = dict(display='none')
            drop_menu_style = None
            drop_menu_label = f"Welcome, {portal_user.first_name()}"
            can_commit = portal_user.can_commit_to_database()
            is_admin = portal_user.is_admin()

    brand_link = dbc.NavbarBrand("Lisberger Data Portal", href="/explore")
    lab_link = html.A("[Courtesy of the Lisberger lab at Duke University]",
                      href="https://www.neuro.duke.edu/research/faculty-labs/lisberger-lab", target="_blank",
                      style=dict(color='white'))
    navbar = dbc.Navbar(
        dbc.Container([
            dbc.Row([
                dbc.Col(brand_link, width='auto', class_name='mr-2'),
                dbc.Col(lab_link, width='auto')
            ], class_name='g-0', align='center'),
            dbc.Row(
                [
                    dbc.Col(dbc.Button("Login", id=_LOGIN_ID, color="info", style=login_btn_style, n_clicks=0),
                            width="auto"),
                    dbc.Col(
                        dbc.DropdownMenu(
                            children=[
                                dbc.DropdownMenuItem("What do you want to do?", header=True),
                                dbc.DropdownMenuItem("Explore the database", href="/explore"),
                                dbc.DropdownMenuItem(divider=True),
                                dbc.DropdownMenuItem("Commit experiment sessions (access restricted)",
                                                     id=_LINK_COMMIT_ID, href="/commit",
                                                     disabled=not can_commit),
                                dbc.DropdownMenuItem("Download history (access restricted)", href='/downloads',
                                                     id=_LINK_DOWNLOADS_ID, disabled=not can_commit),
                                dbc.DropdownMenuItem(divider=True),
                                dbc.DropdownMenuItem("Portal Administration (access restricted)", id=_LINK_ADMIN_ID,
                                                     href="/manage", disabled=not is_admin),
                                dbc.DropdownMenuItem(divider=True),
                                dbc.DropdownMenuItem("Update your profile", href="/user_profile"),
                                dbc.DropdownMenuItem("Logout", id=_LOGOUT_ID, n_clicks=0)
                            ],
                            id=_NAV_MENU_ID, align_end=True, label=drop_menu_label, color="info", style=drop_menu_style
                        ),
                        width="auto"
                    )
                ],
                class_name="g-0 ml-auto flex-nowrap mt-3 mt-md-0", align="center"
            )
        ], fluid=True),
        color="primary",
        dark=True,
    )

    # the div encapsulating page content is populuated each time the URL changes
    return html.Div([
        dcc.Location(id=_URL_ID, refresh=False),
        #  dcc.Location(id=_REDIRECT_ID, refresh=True),
        dcc.Interval(id=_AUTH_INTV_ID, disabled=False, interval=10000),
        navbar,
        modal_login,
        html.Div(id=_PAGE_CONTENT_ID)
    ])


# by implementing the front-page layout as a function, we can always check for a logged-in user to get the nav bar right
app.layout = _serve_layout


@callback(
    [Output(_PAGE_CONTENT_ID, 'children'), Output(_URL_ID, 'href'), Output(_URL_ID, 'refresh')],
    [Input(_URL_ID, 'pathname'), Input(_AUTH_INTV_ID, 'n_intervals')], [State(_URL_ID, 'href')]
)
def display_page(pathname, n_intervals, current_href):
    ctx = callback_context
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if (ctx.triggered is not None) else ""

    # at regular intervals we check to see if we're on a restricted-access page, but the user is either not logged in
    # or doesn't have the required access. When that happens, redirect to the home page.
    if (trigger_id == _AUTH_INTV_ID) and (n_intervals is not None):
        url_parts = urlparse(current_href) if isinstance(current_href, str) else ""
        if url_parts.path in _PUBLIC_ENDPOINTS:
            return no_update, no_update, False
        can_commit = is_admin = is_logged_in = False
        if flask_login.current_user.is_authenticated:
            portal_user = load_authorized_user(flask_login.current_user.get_id())
            if portal_user:
                is_logged_in = True
                can_commit = portal_user.can_commit_to_database()
                is_admin = portal_user.is_admin()
        if ((url_parts.path == '/commit') and not can_commit) or \
                ((url_parts.path == '/manage') and not is_admin) or \
                ((url_parts.path == '/downloads') and not can_commit) or (not is_logged_in):
            url_parts = [(part if i != 2 else '/explore') for i, part in enumerate(url_parts)]
            return no_update, urlunparse(url_parts), True
        return no_update, no_update, False

    redirect = False
    if pathname == '/explore':
        layout = explore.serve_layout()
    else:
        can_commit = is_admin = is_logged_in = False
        if flask_login.current_user.is_authenticated:
            portal_user = load_authorized_user(flask_login.current_user.get_id())
            if portal_user:
                is_logged_in = True
                can_commit = portal_user.can_commit_to_database()
                is_admin = portal_user.is_admin()
        if pathname == '/commit':
            layout = commit.serve_layout() if can_commit else None
            redirect = not can_commit
        elif pathname == '/user_profile':
            layout = user_profile.serve_layout() if is_logged_in else None
            redirect = not is_logged_in
        elif pathname == '/manage':
            layout = manage.serve_layout() if is_admin else None
        elif pathname == '/downloads':
            layout = download_history.serve_layout() if can_commit else None
            redirect = not can_commit
        else:
            layout = explore.serve_layout()
            redirect = not (pathname in ['/', '/explore'])   # eg, someone enters a bogus path manually
    update_href = no_update
    if redirect:
        url_parts = urlparse(current_href)
        update_href = urlunparse([(part if i != 2 else '/explore') for i, part in enumerate(url_parts)])
    return layout, update_href, redirect


@app.callback(
    [Output(_LOGIN_SUBMIT_ID, 'disabled'), Output(_LOGIN_USERNAME_ID, 'valid'), Output(_LOGIN_USERNAME_ID, 'invalid'),
     Output(_LOGIN_PASSWORD_ID, 'valid'), Output(_LOGIN_PASSWORD_ID, 'invalid')],
    [Input(_LOGIN_USERNAME_ID, 'value'), Input(_LOGIN_PASSWORD_ID, 'value')],
    [State(_LOGIN_USERNAME_ID, 'value'), State(_LOGIN_PASSWORD_ID, 'value')]
)
def enable_login_submit(*args):
    ctx = callback_context
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if (ctx.triggered is not None) else ""
    uname, pwd = None, None
    if trigger_id == _LOGIN_USERNAME_ID:
        uname, pwd = args[0], args[3]
    elif trigger_id == _LOGIN_PASSWORD_ID:
        uname, pwd = args[2], args[1]
    uname_set, uname_valid = ((uname is not None) and (len(uname) > 0)), validate_username(uname)
    pwd_set, pwd_valid = ((pwd is not None) and (len(pwd) > 0)), (validate_password(pwd) is None)
    return not (uname_valid and pwd_valid), uname_set and uname_valid, uname_set and not uname_valid, \
        pwd_set and pwd_valid, pwd_set and not pwd_valid


@app.callback(
    [Output(_LOGIN_MODAL_ID, 'is_open'), Output(_LOGIN_USERNAME_ID, 'value'), Output(_LOGIN_PASSWORD_ID, 'value'),
     Output(_NAV_MENU_ID, 'label'), Output(_NAV_MENU_ID, 'style'), Output(_LOGIN_ID, 'style'),
     Output(_LINK_COMMIT_ID, 'disabled'), Output(_LINK_DOWNLOADS_ID, 'disabled'), Output(_LINK_ADMIN_ID, 'disabled'),
     Output(_LOGIN_ALERT_ID, 'children'), Output(_LOGIN_ALERT_ID, 'is_open'), Output(_URL_ID, 'pathname')],
    [Input(_LOGIN_ID, 'n_clicks'), Input(_LOGIN_SUBMIT_ID, 'n_clicks'), Input(_LOGIN_CANCEL_ID, 'n_clicks'),
     Input(_LOGOUT_ID, 'n_clicks')],
    [State(_LOGIN_USERNAME_ID, 'value'), State(_LOGIN_PASSWORD_ID, 'value')]
)
def login_callback(*args):
    ctx = callback_context
    out = [no_update] * 12
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if (ctx.triggered is not None) else ""

    if trigger_id == _LOGIN_ID:
        out[0] = True
        out[1] = out[2] = ""
        out[10] = False
    elif trigger_id == _LOGIN_SUBMIT_ID:
        username = args[4] if isinstance(args[4], str) else ""
        password = args[5] if isinstance(args[5], str) else ""
        error_msg = authenticate_portal_user(username, password)
        portal_user = None
        if error_msg is None:
            portal_user = load_authorized_user(username)
            if portal_user is None:
                error_msg = 'Failed to find user account record'
            else:
                flask_login.login_user(portal_user)
        if portal_user:  # successful login!
            get_application_logger().info(f"{username} logged in successfully")
            out[0] = False
            out[1] = out[2] = ""
            out[3] = f"Welcome, {portal_user.first_name()}"
            out[4] = None
            out[5] = dict(display='none')
            out[6] = not portal_user.can_commit_to_database()
            out[7] = not portal_user.can_commit_to_database()
            out[8] = not portal_user.is_admin()
            out[9] = ""
            out[10] = False
            out[11] = '/explore'
        else:
            get_application_logger().warning(f"Unsuccessful login attempt ({error_msg})")
            out[9] = error_msg
            out[10] = True
    elif trigger_id == _LOGIN_CANCEL_ID:
        out[0] = False
        out[1] = out[2] = ""
        out[10] = False
    elif trigger_id == _LOGOUT_ID:
        if flask_login.current_user.is_authenticated:
            get_application_logger().info(f"{flask_login.current_user.get_id()} logged out")
        flask_login.logout_user()
        out[3] = "Welcome"
        out[4] = dict(display='none')
        out[5] = None
        out[6] = True
        out[7] = True
        out[8] = True
        out[11] = '/explore'
    return tuple(out)


# To serve the backend app with GUnicorn, use this to supply the Flask application instance
def get_app():
    """
    The Flask application instance for the portal backend. When serving the backend with GUnicorn, use this to supply
    the application instance.
    """
    return app.server


# To run the backend on the Flask development server, run 'python index.py'
if __name__ == '__main__':
    get_application_logger().info("Starting portal app on Flask development server.")
    # force single-threaded server to avoid thread conflicts in servicing requests.
    app.run_server(host='0.0.0.0', port='8050', debug=True, threaded=False)
