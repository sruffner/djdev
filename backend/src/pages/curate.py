import sys
import dash
import dash_html_components as html
import dash_bootstrap_components as dbc
from dash.dependencies import Input, Output, State
from app import app

import pages.curate_panels as cp
import database.table_views as tv

user_panel = cp.UserPanel(app)
subj_panel = cp.SubjectPanel(app)
rig_panel = cp.RigPanel(app)
brain_panel = cp.BrainRegionPanel(app)
study_panel = cp.StudyPanel(app)
tab_to_panel = {f"{p.id_prefix()}_tab": p for p in [user_panel, subj_panel, rig_panel, brain_panel, study_panel]}

tabs_card = dbc.Card(
    [
        dbc.CardHeader(
            dbc.Tabs(
                [dbc.Tab(label=panel.tab_label(), tab_id=tab_id) for tab_id, panel in tab_to_panel.items()],
                id="tabs",
                card=True, persistence=True, persistence_type="session",
                active_tab=f"{user_panel.id_prefix()}_tab",
            )
        ),
        dbc.CardBody(id="sel-tab-content", children=[])
    ]
)

layout = html.Div([
    dbc.Container([
        dbc.Row([
            dbc.Col(
                html.H3("Curate the laboratory database", className="text-center"),
                className="mb-3 mt-3")
        ]),
        dbc.Row([dbc.Col(html.H5(children='*** UNDER CONSTRUCTION ***'), className="mb-2")]),
        dbc.Row([
            dbc.Button("Reset database", id="reset_db_btn", color="primary", className="mr-2 mb-3"),
            dbc.Button("Seed database", id="seed_db_btn", color="primary", className="mr-2 mb-3")
        ]),
        tabs_card
    ])
])


@app.callback(Output("sel-tab-content", "children"),
              [Input("tabs", "active_tab"), Input("reset_db_btn", "n_clicks"), Input("seed_db_btn", "n_clicks")],
              [State("tabs", "active_tab")])
def update_tab_content(active_tab, *args):
    ctx = dash.callback_context
    out = dash.no_update
    btn_id = ctx.triggered[0]['prop_id'].split('.')[0] if ctx.triggered else ''
    curr_active_tab = args[2]
    if btn_id.find("reset_db_btn") > -1:
        msg = tv.reset_database()
        print(f"Reset database, msg={msg}", file=sys.stdout, flush=True)
        if curr_active_tab:
            out = tab_to_panel[curr_active_tab].layout()
    elif btn_id.find("seed_db_btn") > -1:
        msg = tv.seed_database()
        print(f"Seeded database, msg={msg}", file=sys.stdout, flush=True)
        if curr_active_tab:
            out = tab_to_panel[curr_active_tab].layout()
    else:
        tabpane = None
        try:
            tabpane = tab_to_panel[active_tab]
        except Exception:
            pass
        out = tabpane.layout() if tabpane else html.Div(["No tab selected"])
    return out
