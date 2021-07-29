import dash
import dash_html_components as html
import dash_bootstrap_components as dbc
from dash.dependencies import Input, Output
from app import app

import pages.curate_panels as cp

subj_panel = cp.SubjectPanel(app)
rig_panel = cp.RigPanel(app)
brain_panel = cp.BrainAreaPanel(app)
n_type_panel = cp.NeuronTypePanel(app)
study_panel = cp.StudyPanel(app)
tab_to_panel = {f"{p.id_prefix()}_tab": p
                for p in [subj_panel, rig_panel, brain_panel, n_type_panel, study_panel]}

tabs_card = dbc.Card(
    [
        dbc.CardHeader(
            dbc.Tabs(
                [dbc.Tab(label=panel.tab_label(), tab_id=tab_id) for tab_id, panel in tab_to_panel.items()],
                id="tabs",
                card=True, persistence=True, persistence_type="session",
                active_tab=f"{subj_panel.id_prefix()}_tab",
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
        dbc.Row([dbc.Col(html.H5(children='*** UNDER CONSTRUCTION ***'), className="mb-3")]),
        tabs_card
    ])
])


@app.callback(Output("sel-tab-content", "children"),
              [Input("tabs", "active_tab")])
def update_tab_content(active_tab):
    out = dash.no_update
    if active_tab:
        tabpane = None
        try:
            tabpane = tab_to_panel[active_tab]
        except Exception:
            pass
        out = tabpane.layout() if tabpane else html.Div(["No tab selected"])
    return out
