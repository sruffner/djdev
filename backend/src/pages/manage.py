"""
manage.py: Portal administration functions.

This page should only be accessible when a user with 'admin'-level privileges is currently logged into the portal. It
exposes various portal administation functions using an accordion-style widget. The content of each accordion item is
rendered in a different module:

    - curate.py: Curate database tables with lab metadata (subjects, experiment rigs, research projects, etc).
    - manage_users.py: User management functions.
    - manage_repo.py: View the contents of the portal backup repository on S3.

@created: 18apr2022
@author: sruffner
"""
from typing import Optional

import flask_login
from dash import html
import dash_bootstrap_components as dbc

from app import PortalUser, load_authorized_user
from pages import manage_users, manage_repo, curate, manage_app_log


def serve_layout() -> html.Div:
    # client must be authenticated with 'admin'-level privileges to view this page
    portal_user: Optional[PortalUser] = None
    if flask_login.current_user.is_authenticated:
        portal_user = load_authorized_user(flask_login.current_user.get_id())
    is_admin = (portal_user is not None) and portal_user.is_admin()
    if not is_admin:
        return html.Div("Access denied. You must be logged in with administrator privileges to view this content.",
                        className='mx-5 my-5')

    content_div = html.Div(dbc.Accordion([
        dbc.AccordionItem(curate.layout, title="Curate Portal Content"),
        dbc.AccordionItem(manage_users.serve_layout(), title='Manage Users'),
        dbc.AccordionItem(manage_repo.serve_layout(), title='View Backup Repository on S3'),
        dbc.AccordionItem(manage_app_log.serve_layout(), title='Portal Server Message Logs')
    ], start_collapsed=True, flush=True))

    card = dbc.Card([
        dbc.CardHeader("Portal Administration"),
        dbc.CardBody([content_div]),
    ], class_name='mx-5 my-5')

    return html.Div([card])
