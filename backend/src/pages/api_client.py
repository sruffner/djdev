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
import types
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


def _get_markdown_for_module(mod: types.ModuleType) -> str:
    """
    Auto-generate basic documentation for the specified module in markdown format.

    This function attempts to display documentation in a manner similar to what the standard module `pydoc` supplies,
    but in markdown format (conforming to the CommonMark spec) rather than plain text or HTML. It is NOT a complete
    solution, as it only displays docstrings for the module, any module functions, and any module classes. It will not
    handle all possible Python types correctly (but it is adequate for generating markdown documentation for the main
    modules in the sglportalapi package.

    Args:
        mod: The module.
    Returns:
        Generated module documentation in markdown format.
    """
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
                    doc = inspect.getdoc(value)
                    lines.append(f"_{prefix}**{name}**{signature}_:{suffix}")
                    lines.append(f"```text\n{doc}\n```\n" if doc else "```text\nNo documentation found.\n```\n")
                except Exception:
                    lines.append(f"_{cls.__name__}.**{name}**_: Unable to generate documentation.")
        lines.append("_______\n")

    module_functions = []
    for k, v in inspect.getmembers(mod, inspect.isroutine):
        if (inspect.getmodule(v) == mod) and (not inspect.isbuiltin(v)) and not k.startswith('_'):
            module_functions.append(v)
    if len(module_functions) > 0:
        lines.append(f'##### Functions')
        for func in module_functions:
            try:
                signature = str(inspect.signature(func)).replace(single_quote, '').replace('[', '\\['). \
                    replace(']', '\\]')
                doc = inspect.getdoc(func)
                lines.append(f"*def **{func.__name__}**{signature}*:\n")
                lines.append(f"```text\n{doc}\n```\n" if doc else "```text\nNo documentation found.\n```\n")
            except Exception:
                lines.append(f"*def **{func.__name__}**: Unable to generate documentation.*")
            lines.append("_______\n")

    return '\n'.join(lines)


_DOWNLOAD_BTN: str = 'api_btn_download'
""" ID of 'Download API Client' button. """
_DOWNLOADER_ID: str = 'api_downloader'
""" ID of the Dash Download component that manages download of the sglportalapi package installation (wheel) file. """
_TABS_ID: str = 'api_tabs'
""" ID of the Dash Bootstrap Tabs component in which documentation is displayed for the sglportalapi package. """
_TAB_README: str = 'api_readme_tab'
""" Tab on which the sglportalapi README is displayed."""
_TAB_CHANGELOG: str = 'api_changelog_tab'
""" Tab on which the sglpportalapi CHANGELOG is displayed. """
_TAB_CLIENTSIDE: str = 'api_clientside_tab'
""" Tab displaying documentation for the sglportalapi.clientside module. """
_TAB_DATA_CONTAINER: str = 'api_data_container_tab'
""" Tab displaying documentation for the sglportalapi.data_containers module. """
_TAB_MAESTRO: str = 'api_maestro_tab'
""" Tab displaying documentation for the sglportalapi.maestro module. """
_MARKDOWN_ID: str = 'api_markdown'
""" ID of Dash Markdown component in which documentation is rendered. """


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
    analyze experimental data directly from a Python interactive console or within your own custom Python script. As a 
    registered user, you can download the package and use it as you wish. ***Note that every package download
    and all API requests are recorded in an effort to protect the provenance of the experimental data
    stored in the portal.***
    ''', style=dict(color='black', backgroundColor='lightsteelblue'))

    download_row = dbc.Row([
        dbc.Button('Download API Client Package', id=_DOWNLOAD_BTN, size='lg', disabled=not enable),
        dcc.Download(id=_DOWNLOADER_ID)
    ], class_name='d-grid col-4 mx-auto my-4')

    tabs = dbc.Tabs([
        dbc.Tab(label="README", tab_id=_TAB_README),
        dbc.Tab(label="Changelog", tab_id=_TAB_CHANGELOG),
        dbc.Tab(label="API Doc: clientside", tab_id=_TAB_CLIENTSIDE),
        dbc.Tab(label="API Doc: data_containers", tab_id=_TAB_DATA_CONTAINER),
        dbc.Tab(label='API Doc: maestro', tab_id=_TAB_MAESTRO)
    ], id=_TABS_ID, active_tab=_TAB_README)
    content_markdown = dcc.Markdown(id=_MARKDOWN_ID, children=sglportalapi.readme(),
                                    style=dict(maxHeight='600px', overflowY='scroll',
                                               border='1px solid rgba(176,196,222,0.5'))

    card = dbc.Card([
        dbc.CardHeader("API Client"),
        dbc.CardBody([explainer, download_row, tabs, content_markdown]),
    ], class_name='mx-5 my-5')

    return html.Div([card])


@callback(Output(_MARKDOWN_ID, "children"), [Input(_TABS_ID, "active_tab")])
def update_tab_content(active_tab):
    if active_tab == _TAB_README:
        return sglportalapi.readme()
    elif active_tab == _TAB_CHANGELOG:
        return sglportalapi.changelog()
    elif active_tab == _TAB_CLIENTSIDE:
        return _get_markdown_for_module(sglportalapi.clientside)
    elif active_tab == _TAB_DATA_CONTAINER:
        return _get_markdown_for_module(sglportalapi.data_containers)
    elif active_tab == _TAB_MAESTRO:
        return _get_markdown_for_module(sglportalapi.maestro)
    else:
        return '***No tab selected***'


# TODO: IMPLEMENT -- Need to record every API package download.
# noinspection PyUnusedLocal
@callback(Output(_DOWNLOADER_ID, "data"), [Input(_DOWNLOAD_BTN, "n_clicks")], prevent_initial_call=True)
def download_api_client(n_clicks):
    get_application_logger().info("Downloading API package")
    p = Path(__file__)
    p = Path(p.parent.parent, 'sglportalapi', 'dist', 'sglportalapi-0.2.0-py3-none-any.whl')
    if not p.is_file():
        get_application_logger().error(f"API package wheel not found at {str(p.absolute())}")
    try:
        return dcc.send_file(path=p)
    except Exception as e:
        get_application_logger().error(f"Download failed: {str(e)}", exc_info=True)
