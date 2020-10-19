import dash_html_components as html
import dash_bootstrap_components as dbc

layout = html.Div([
    dbc.Container([
        dbc.Row([
            dbc.Col(
                html.H1("Commit an experiment session to the laboratory database", className="text-center"),
                className="mb-5 mt-5")
        ]),

        dbc.Row([
            dbc.Col(html.H5(children='*** UNDER CONSTRUCTION ***'), className="mb-4")
            ])
    ])

])
