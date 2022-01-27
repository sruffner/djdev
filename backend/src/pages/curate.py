import dash
import dash_html_components as html
import dash_core_components as dcc
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

markdown = dcc.Markdown('''
    Users with administrative-level access to the portal can add and/or update information used to categorize and 
    search for datasets stored in the lab database. **The system will generally prevent you from deleting any 
    information that would indirectly cause the removal of experimental data; nevertheless, avoid removing any 
    metadata unless you are sure it is safe!**
    ''', className='mt-1 mb-2')
container_card = dbc.Card([
    dbc.CardHeader("Curate the laboratory database"),
    dbc.CardBody([markdown, tabs_card])
], className='w-75 mx-auto mt-5')

layout = html.Div([container_card])


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
