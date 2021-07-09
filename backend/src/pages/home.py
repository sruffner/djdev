import dash_html_components as html
import dash_bootstrap_components as dbc


def serve_layout(can_curate: bool, can_commit: bool) -> html.Div:
    """
    Serve the layout for the portal's "Home" page.
    Args:
        can_curate: True if a user is logged in with curate access to the portal database.
        can_commit: True if a user is logged in with commit access to the portal database.
    Returns:
        An HTML Div rendering the "Home" page. The arguments are used to selectively enable/disable links to restricted
            pages in the portal.
    """
    task_row1 = dbc.Row([
        dbc.Col(
            dbc.Card(
                children=[
                    dbc.Button("Explore", href="/explore", color="primary", className="mt-3"),
                    html.H4("Explore the lab database for specific datasets and related information (login "
                            "for downloads only)", className="text-center mt-3")],
                body=True, color="dark", outline=True),
            width=4, className="mb-4"),

        dbc.Col(
            dbc.Card(
                children=[
                    dbc.Button("Curate", href="/curate", color="primary", className="mt-3", disabled=not can_curate),
                    html.H4("Annotate the lab's database with information on specific studies, etc. (login required)",
                            className="text-center mt-3")],
                body=True, color="dark", outline=True),
            width=4, className="mb-4"),

        dbc.Col(
            dbc.Card(
                children=[
                    dbc.Button("Commit", href="/commit_session", color="primary", className="mt-3",
                               disabled=not can_commit),
                    html.H4("Commit raw data from a single laboratory experiment session (login required)",
                            className="text-center mt-3")],
                body=True, color="dark", outline=True),
            width=4, className="mb-4")
        ], className="mb-5 d-flex flex-row")

    return html.Div([
        dbc.Container([
            dbc.Row([
                dbc.Col(
                    html.H1("Welcome to the Lisberger Lab Data Portal", className="text-center"),
                    className="mb-5 mt-5")
            ]),

            dbc.Row([
                dbc.Col(html.H5(children='A one-liner description of purpose goes here'), className="mb-4")
                ]),

            dbc.Row([
                dbc.Col(
                    html.P(children='A longer description goes here, or an abstract of lab research?'),
                    className="mb-5")
            ]),

            task_row1,

            html.A("Courtesy of the Lisberger laboratory at Duke University",
                   href="https://www.neuro.duke.edu/research/faculty-labs/lisberger-lab", target="_blank")

        ])

    ])
