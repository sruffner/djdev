import dash_core_components as dcc
import dash_html_components as html
from dash.dependencies import Input, Output
import dash_bootstrap_components as dbc

from app import app
# import all pages in the app
from database.manager import DataBaseManager
from pages import home, curate, commit_session, explore

navbar = dbc.NavbarSimple(
    children=[
        dbc.NavItem(dbc.NavLink("Home", href="/home")),
        dbc.DropdownMenu(
            children=[
                dbc.DropdownMenuItem("What do you want to do?", header=True),
                dbc.DropdownMenuItem("Explore the database", href="/explore"),
                dbc.DropdownMenuItem("Curate lab information", href="/curate"),
                dbc.DropdownMenuItem("Commit experiment session", href="/commit_session")
            ],
            nav=True,
            in_navbar=True,
            label="Tasks",
        )
    ],
    brand='Lisberger Data Portal',
    brand_href="/home",
    color="primary",
    dark=True,
)

app.layout = html.Div([
    dcc.Location(id='url', refresh=False),
    navbar,
    html.Div(id='page-content')
])


@app.callback(Output('page-content', 'children'), [Input('url', 'pathname')])
def display_page(pathname):
    if pathname == '/curate':
        return curate.layout
    elif pathname == '/commit_session':
        return commit_session.layout
    elif pathname == '/explore':
        return explore.layout
    else:
        return home.layout


if __name__ == '__main__':
    mgr = DataBaseManager()
    msg = mgr.on_startup()
    if msg:
        print(f"===> {msg}", flush=True)
    app.run_server(host='0.0.0.0', port='8050', debug=True)
