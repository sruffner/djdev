"""
explore.py: The starting page in the Lisberger lab's database portal app devoted to searching database
    content (/explore endpoint)

This page merely presents a "welcome message" and a set of 3 Bootstrap Cards that link to detail pages for exploring the
different research projects underway in the laboratory; searching for behavioral and/or neuronal data sets by experiment
session; and searching the table of all neural unit recordings in the database.

DEV NOTE: As of 15Jun2021, only the neurons.py endpoint is available. The link buttons for the other two cards are
disabled.

@author: sruffner
@created: 15jun2021
"""
import dash_html_components as html
import dash_bootstrap_components as dbc

cards = dbc.CardDeck([
    dbc.Card([
        dbc.CardHeader("Research Projects"),
        dbc.CardBody(
            [
                html.P("Explore current research projects in the laboratory. Review experiments and stimulus "
                       "paradigms, related publications, and recorded data sets."),
                dbc.Button("Go to Projects page", href="/studies", color="primary", className="mt-auto",
                           disabled=True),
            ]
        )
    ]),
    dbc.Card([
        dbc.CardHeader("Experimental Sessions"),
        dbc.CardBody(
            [
                html.P("Review behavioral and/or neuronal data sets from any individual experiment session stored "
                       "in the database. "),
                dbc.Button("Go to Sessions page", href="/sessions", color="primary", className="mt-auto",
                           disabled=False),
            ]
        )
    ]),
    dbc.Card([
        dbc.CardHeader("Neurons"),
        dbc.CardBody(
            [
                html.P("Search all neural unit recordings available in the laboratory's database. Mean firing rate, "
                       "autocorrelogram, and inter-spike interval analyses available for many units."),
                dbc.Button("Go to Neurons page", href="/neurons", color="primary", className="mt-auto"),
            ]
        )
    ])
])


layout = html.Div([
    dbc.Container([
        dbc.Row([
            dbc.Col(
                html.H1("Welcome to the Lisberger Lab Data Portal", className="text-center"),
                className="mb-5 mt-5")
        ]),

        dbc.Row([
            dbc.Col(
                html.H3("Explore datasets and other content within the Lisberger laboratory database",
                        className="text-center"),
                className="mb-3 mt-3")
        ]),

        dbc.Row([
            dbc.Col(
                html.P(children='A longer description goes here, or an abstract of lab research?'),
                className="mb-5")
        ]),

        cards,

        dbc.Row([
            dbc.Col(
                html.A("Courtesy of the Lisberger laboratory at Duke University",
                       href="https://www.neuro.duke.edu/research/faculty-labs/lisberger-lab", target="_blank"),
                className="mt-5")
        ])
    ])
])
