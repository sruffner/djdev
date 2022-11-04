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
import sys
import types
from typing import Optional, List, Tuple

from dash import html, dcc, callback, Output, Input
import dash_bootstrap_components as dbc
import flask_login
from dash.exceptions import PreventUpdate

import sglportalapi.clientside
import sglportalapi.data_containers
import sglportalapi.maestro
from app import PortalUser, load_authorized_user
from config.app_logging import get_application_logger
from database.log_ops import log_api_request


def _get_module_functions(mod: types.ModuleType) -> List[str]:
    """
    Get the names of all functions defined in the specified Python module. This method only finds non-builtin
    functions belonging to the module and not starting with '_'.

    Args:
        mod: The module.
    Returns:
        List of module function names, possibly empty.
    """
    out = []
    for name, func in inspect.getmembers(mod, inspect.isroutine):
        if (inspect.getmodule(func) == mod) and (not inspect.isbuiltin(func)) and not name.startswith('_'):
            out.append(name)
    return out


def _get_module_classes(mod: types.ModuleType) -> List[str]:
    """
    Get the names of all classes defined in the specified Python module. Module classes starting with '_' are ignored.

    Args:
        mod: The module.
    Returns:
        List of module class names, possibly empty.
    """
    out = []
    for name, cls in inspect.getmembers(mod, inspect.isclass):
        if (inspect.getmodule(cls) == mod) and not name.startswith('_'):
            out.append(name)
    return out


def _get_module_markdown(mod_name: str) -> str:
    """
    Get top-level description of the specified module.

    Args:
        mod_name: The module name.
    Returns:
        The "doc string" for the module, in markdown format (CommonMark spec). If unable to locate module or its doc
            string, returns "No documentation found."
    """
    mod = sys.modules.get(mod_name)
    if mod is None:
        return "***No documentation found.***"

    lines: List[str] = list()
    lines.append(f'### Module\n&nbsp;&nbsp;&nbsp;***{mod.__name__}***')
    lines.append(f'### Description\n{mod.__doc__}')
    return '\n'.join(lines)


def _get_module_function_markdown(mod_name: str, func_name: str) -> str:
    """
    Get documentation for the specified module function, including the function's signature and its Python doc-string.

    Args:
        mod_name: The module name.
        func_name: The function name.
    Returns:
        The documentation in markdown format (CommonMark spec), or "No documentation found".
    """
    mod = sys.modules.get(mod_name)
    if mod is None:
        return "***No documentation found.***"

    func = None
    for name, v in inspect.getmembers(mod, inspect.isroutine):
        if (name == func_name) and (inspect.getmodule(v) == mod) and \
                (not inspect.isbuiltin(v)) and not name.startswith('_'):
            func = v
            break
    if func is None:
        return '***No documentation found.***'

    lines: List[str] = list()
    try:
        single_quote = "'"
        signature = str(inspect.signature(func)).replace(single_quote, '').replace('[', '\\['). \
            replace(']', '\\]')
        doc = inspect.getdoc(func)
        lines.append(f"*def **{func.__name__}**{signature}*:\n")
        lines.append(f"```text\n{doc}\n```\n" if doc else "```text\nNo documentation found.\n```\n")
    except Exception:
        lines.append(f"*def **{func.__name__}**: Unable to generate documentation.*")
    return '\n'.join(lines)


def _get_module_class_markdown(mod_name: str, cls_name: str) -> str:
    """
    Get documentation for the specified module class, including:
     - Class-level doc-string.
     - Signature and doc-string for each static method, class method, instance method, and property of the class.

    Args:
        mod_name: The module name.
        cls_name: The class name.
    Returns:
        The documentation in markdown format (CommonMark spec), or "No documentation found".
    """
    mod = sys.modules.get(mod_name)
    if mod is None:
        return "***No documentation found.***"

    cls = None
    for name, v in inspect.getmembers(mod, inspect.isclass):
        if (name == cls_name) and (inspect.getmodule(v) == mod) and not name.startswith('_'):
            cls = v
            break
    if cls is None:
        return '***No documentation found.***'

    single_quote = "'"
    lines: List[str] = list()
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

    return '\n'.join(lines)


_DOWNLOAD_BTN: str = 'api_btn_download'
""" ID of 'Download API Client' button. """
_DOWNLOADER_ID: str = 'api_downloader'
""" ID of the Dash Download component that manages download of the sglportalapi package installation (wheel) file. """
_DOC_ITEM_LIST: str = 'api_doc_items'
""" A Bootstrap component displaying list of modules, classes, function, etc for which documentation is available. """
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
    package documented here implements the low-level details of sending HTTP requests to the API endpoints 
    and unpacking the responses. It also defines the various data objects that may be returned in a 
    response -- experiment session or neural unit metadata, trial protocol definitions, and trial 
    response data sets.
    
    With this package and a secure Internet connection with access to the portal's website, you can retrieve and
    analyze experimental data directly from a Python interactive console or within your own custom Python script. As a 
    registered user, you can download the package and use it as you wish. ***Note that every package download
    and all API requests are recorded in an effort to protect the provenance of the experimental data
    stored in the portal.***
    ''', style=dict(color='black', backgroundColor='lightsteelblue', padding='0.5em'))

    download_row = dbc.Row([
        dbc.Button('Download API Client Package', id=_DOWNLOAD_BTN, size='lg', disabled=not enable),
        dcc.Download(id=_DOWNLOADER_ID)
    ], class_name='d-grid col-4 mx-auto my-4')

    content_markdown = dcc.Markdown(id=_MARKDOWN_ID, children=sglportalapi.readme(),
                                    style=dict(maxHeight='600px', overflowY='scroll',
                                               border='1px solid rgb(176,196,222)', padding='0.5em'))

    options: List[Tuple[str, str]] = [("README", "README"), ("CHANGELOG", "CHANGELOG")]
    for mod in [sglportalapi.clientside, sglportalapi.data_containers, sglportalapi.maestro]:
        options.append((mod.__name__, f"M {mod.__name__}"))
        options.extend([(f"\u2003{name}", f"F {mod.__name__} {name}") for name in _get_module_functions(mod)])
        options.extend([(f"\u2003{name}", f"C {mod.__name__} {name}") for name in _get_module_classes(mod)])

    list_group = dbc.RadioItems(
        id=_DOC_ITEM_LIST,
        class_name="btn-group-vertical radio-group",
        inputClassName="btn-check",
        labelClassName="btn btn-outline-primary",
        labelCheckedClassName="active",
        options=[{"label": lbl, "value": val} for lbl, val in options],
        value="README",
        style=dict(display='block', maxHeight='600px', overflowY='scroll', border='1px solid rgb(176,196,222)')
    )

    documentation_section = dbc.Row([dbc.Col(list_group, width=3), dbc.Col(content_markdown, width=9)])

    card = dbc.Card([
        dbc.CardHeader("API Client"),
        dbc.CardBody([explainer, download_row, html.Hr(), documentation_section]),
    ], class_name='mx-5 my-5')

    return html.Div([card])


@callback(Output(_MARKDOWN_ID, "children"), [Input(_DOC_ITEM_LIST, "value")], prevent_initial_call=True)
def display_documentation(value):
    if value == 'README':
        return sglportalapi.readme()
    elif value == 'CHANGELOG':
        return sglportalapi.changelog()
    parts = value.split()
    if parts[0] == 'M':
        return _get_module_markdown(parts[1])
    elif parts[0] == 'F':
        return _get_module_function_markdown(parts[1], parts[2])
    elif parts[0] == 'C':
        return _get_module_class_markdown(parts[1], parts[2])
    else:
        return "***No documentation found.***"


# noinspection PyUnusedLocal
@callback(Output(_DOWNLOADER_ID, "data"), [Input(_DOWNLOAD_BTN, "n_clicks")], prevent_initial_call=True)
def download_api_client(n_clicks):
    # need to make sure current user is still logged in, and that wheel file exists
    wheel_path = sglportalapi.path_to_package_wheel()
    portal_user: Optional[PortalUser] = None
    if flask_login.current_user.is_authenticated:
        portal_user = load_authorized_user(flask_login.current_user.get_id())
    if (portal_user is None) or (wheel_path is None):
        get_application_logger().debug(f"Download aborted because "
                                       f"{'user not logged in' if (portal_user is None) else 'wheel file missing'}")
        raise PreventUpdate

    try:
        out = dcc.send_file(path=wheel_path)
        log_api_request(route='/api_client', username=portal_user.id)
        return out
    except Exception as e:
        get_application_logger().error(f"Download failed: {str(e)}", exc_info=True)
