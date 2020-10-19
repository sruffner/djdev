import dash_html_components as html
# import dash_core_components as dcc
# import dash_table as dt
import dash_bootstrap_components as dbc
from dash.dependencies import Input, Output
from app import app

import pages.curate_panels as cp

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
        dbc.Row([
            dbc.Col(html.H5(children='*** UNDER CONSTRUCTION ***'), className="mb-2")
            ]),
        tabs_card
    ])
])


@app.callback(Output("sel-tab-content", "children"), [Input("tabs", "active_tab")])
def update_tab_content(active_tab):
    tabpane = None
    try:
        tabpane = tab_to_panel[active_tab]
    except Exception:
        pass
    return tabpane.layout() if tabpane else html.Div(["No tab selected"])
