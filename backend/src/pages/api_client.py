"""
api_client.py: Page displaying information about and a download link for the portal API's clientside package

The portal implements a number of API endpoints by which a registered user can -- from a Python interactive console or
their own custom scripts -- retrieve trial responses and other data from the underlying database for further analysis.
For details, see :ref:`api.endpoints`. The :ref:`sglportalapi` package contains the client-side code needed to send and
receive requests to the portal's API endpoints. It defines the data classes for experiment sessions, neuron metadata,
trial protocols, and -- most crucially -- per-trial response data.

Registered users can download the clientside package from this page, which also displays documentation for the package.
The page should only be accessible when a user is authenticated on the portal, because only registered users are
permitted to download and use the package.
"""
import inspect
from pathlib import Path
from typing import Optional, List

from dash import html, dcc, callback, Output, Input
import dash_bootstrap_components as dbc
import flask_login

import sglportalapi.clientside
import sglportalapi.data_containers
import sglportalapi.maestro
from app import PortalUser, load_authorized_user
from config.app_logging import get_application_logger


def _get_documentation(which: str) -> str:
    if which == _TAB_README or which == _TAB_CHANGELOG:
        try:
            p = Path(__file__)
            p = Path(p.parent.parent, 'sglportalapi', 'README.md' if which == _TAB_README else 'CHANGELOG.md')
            with open(p, 'rt') as f:
                content = f.read()
        except Exception as e:
            content = f"Unable to retrieve documentation from file: {str(e)}"
        return content
    elif which == _TAB_CLIENTSIDE:
        return _get_markdown_for_module(sglportalapi.clientside)
    elif which == _TAB_DATA_CONTAINER:
        return _get_markdown_for_module(sglportalapi.data_containers)
    elif which == _TAB_MAESTRO:
        return _get_markdown_for_module(sglportalapi.maestro)
    else:
        return '***No tab selected***'


# TODO: CONTINUE DEVELOPING THIS FUNCTION THAT GENERATES ADEQUATE MARKDOWN DOCUMENTATION FOR the 3 modules we care about
def _get_markdown_for_module(mod) -> str:
    lines: List[str] = list()
    lines.append(f'### Name\n&nbsp;&nbsp;&nbsp;***{mod.__name__}***')
    lines.append(f'### Description\n{mod.__doc__}')
    lines.append("_______\n")

    single_quote = "'"
    module_classes = []
    for k, v in inspect.getmembers(mod, inspect.isclass):
        if (inspect.getmodule(v) == mod) and not k.startswith('_'):
            module_classes.append(v)
    for cls in module_classes:
        lines.append(f"##### class {cls.__name__}:\n")
        inspect.classify_class_attrs(cls)
        doc = inspect.getdoc(cls)
        if doc:
            lines.append(f"{doc}\n")
        attrs = inspect.classify_class_attrs(cls)
        attrs.sort(key=lambda a: a[0])
        for name, kind, home_cls, value in attrs:
            if ((name == '__init__') or ((not name.startswith('_')) and (home_cls == cls))) and \
                    (kind in ['method', 'static method', 'class method', 'property']):
                try:
                    suffix = ""
                    prefix = ""
                    if kind in ['static method', 'class method']:
                        value = value.__func__  # HACK to get around decorators
                        prefix = "@staticmethod " if kind == 'static method' else "@classmethod "
                    elif kind == 'property':
                        suffix = ' (readonly)' if value.fset is None else ''
                        value = value.fget
                        prefix = '@property '
                    signature = str(inspect.signature(value)).replace(single_quote, '').replace('[', '\\['). \
                        replace(']', '\\]')
                    if name == '__init__':
                        name = cls.__name__
                    lines.append(f"_{prefix}**{name}**{signature}_:{suffix}")
                    doc = inspect.getdoc(value)
                    lines.append(f"```text\n{doc}\n```\n" if doc else "```text\nNo documentation found.\n```\n")
                except Exception as e:
                    get_application_logger().debug(f"Unable to get signature for {cls.__name__}.{name}: {str(e)}")
        lines.append("_______\n")

    module_functions = []
    for k, v in inspect.getmembers(mod, inspect.isroutine):
        if (inspect.getmodule(v) == mod) and (not inspect.isbuiltin(v)) and not k.startswith('_'):
            module_functions.append(v)
    if len(module_functions) > 0:
        lines.append(f'##### Functions')
        for func in module_functions:
            signature = str(inspect.signature(func)).replace(single_quote, '').replace('[', '\\['). \
                replace(']', '\\]')
            lines.append(f"*def **{func.__name__}**{signature}*:\n")
            doc = inspect.getdoc(func)
            if doc:
                lines.append(f"```text\n{doc}\n```\n")
            lines.append("_______\n")

    return '\n'.join(lines)


_TABS_ID: str = 'api_tabs'
_TAB_README: str = 'api_readme_tab'
_TAB_CHANGELOG: str = 'api_changelog_tab'
_TAB_CLIENTSIDE: str = 'api_clientside_tab'
_TAB_DATA_CONTAINER: str = 'api_data_container_tab'
_TAB_MAESTRO: str = 'api_maestro_tab'
_MARKDOWN_ID: str = 'api_markdown'


def serve_layout() -> html.Div:
    """
    Serve the layout for the "Download API Client" page. Anonymous users should NOT have access to this page.

    Returns:
        An HTML Div rendering the page.
    """
    portal_user: Optional[PortalUser] = None
    if flask_login.current_user.is_authenticated:
        portal_user = load_authorized_user(flask_login.current_user.get_id())
    enable = (portal_user is not None)

    explainer = dcc.Markdown('''
    This portal implements a number of API "endpoints" by which a client-side application can directly 
    access content in the Lisberger laboratory database outside the context of a web browser. The Python
    package described here implements the low-level details of sending HTTP requests to the API endpoints 
    and unpacking the responses. It also defines the various data objects that may be returned in a 
    response -- experiment session or neural unit metadata, trial protocol definitions, and trial 
    response data sets.
    
    With this package and a secure Internet connection with access to the portal's website, you can retrieve and
    analyze experimental data from a Python interactive console or your own analysis scripts. As a 
    registered user, you can download the package and use it as you wish. ***Note that every package download
    and all API requests are recorded in an effort to protect the provenance of the experimental data
    stored in the portal.***
    ''', style=dict(color='black', backgroundColor='lightsteelblue'))

    download_row = dbc.Row(
        dbc.Button('Download API Client Package', size='lg', disabled=not enable),
        class_name='d-grid col-4 mx-auto my-4',
    )
    tabs = dbc.Tabs([
        dbc.Tab(label="README", tab_id=_TAB_README),
        dbc.Tab(label="Changelog", tab_id=_TAB_CHANGELOG),
        dbc.Tab(label="API Doc: clientside", tab_id=_TAB_CLIENTSIDE),
        dbc.Tab(label="API Doc: data_containers", tab_id=_TAB_DATA_CONTAINER),
        dbc.Tab(label='API Doc: maestro', tab_id=_TAB_MAESTRO)
    ], id=_TABS_ID, active_tab=_TAB_README)
    content_markdown = dcc.Markdown(id=_MARKDOWN_ID, children=_get_documentation(_TAB_README),
                                    style=dict(maxHeight='600px', overflowY='scroll',
                                               border='1px solid rgba(176,196,222,0.5'))

    card = dbc.Card([
        dbc.CardHeader("API Client"),
        dbc.CardBody([explainer, download_row, tabs, content_markdown]),
    ], class_name='mx-5 my-5')

    return html.Div([card])


@callback(Output(_MARKDOWN_ID, "children"), [Input(_TABS_ID, "active_tab")])
def update_tab_content(active_tab):
    return _get_documentation(active_tab)
