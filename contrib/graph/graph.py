#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2016 OpenMarket Ltd
# Copyright (C) 2023 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#

import argparse
import datetime
import html
import json
import urllib.request
from typing import List

import pydot


def make_name(pdu_id: str, origin: str) -> str:
    return f"{pdu_id}@{origin}"


def make_graph(pdus: List[dict], filename_prefix: str) -> None:
    """
    Generate a dot and SVG file for a graph of events in the room based on the
    topological ordering by querying a homeserver.
    """
    pdu_map = {}
    node_map = {}

    origins = set()
    colors = {"red", "green", "blue", "yellow", "purple"}

    for pdu in pdus:
        origins.add(pdu.get("origin"))

    color_map = {color: color for color in colors if color in origins}
    colors -= set(color_map.values())

    color_map[None] = "black"

    for o in origins:
        if o in color_map:
            continue
        try:
            c = colors.pop()
            color_map[o] = c
        except Exception:
            print("Run out of colours!")
            color_map[o] = "black"

    graph = pydot.Dot(graph_name="Test")

    for pdu in pdus:
        name = make_name(pdu.get("pdu_id"), pdu.get("origin"))
        pdu_map[name] = pdu

        t = datetime.datetime.fromtimestamp(float(pdu["ts"]) / 1000).strftime(
            "%Y-%m-%d %H:%M:%S,%f"
        )

        label = (
            "<"
            "<b>%(name)s </b><br/>"
            "Type: <b>%(type)s </b><br/>"
            "State key: <b>%(state_key)s </b><br/>"
            "Content: <b>%(content)s </b><br/>"
            "Time: <b>%(time)s </b><br/>"
            "Depth: <b>%(depth)s </b><br/>"
            ">"
        ) % {
            "name": name,
            "type": pdu.get("pdu_type"),
            "state_key": pdu.get("state_key"),
            "content": html.escape(json.dumps(pdu.get("content")), quote=True),
            "time": t,
            "depth": pdu.get("depth"),
        }

        node = pydot.Node(name=name, label=label, color=color_map[pdu.get("origin")])
        node_map[name] = node
        graph.add_node(node)

    for pdu in pdus:
        start_name = make_name(pdu.get("pdu_id"), pdu.get("origin"))
        for i, o in pdu.get("prev_pdus", []):
            end_name = make_name(i, o)

            if end_name not in node_map:
                print("%s not in nodes" % end_name)
                continue

            edge = pydot.Edge(node_map[start_name], node_map[end_name])
            graph.add_edge(edge)

        # Add prev_state edges, if they exist
        if pdu.get("prev_state_id") and pdu.get("prev_state_origin"):
            prev_state_name = make_name(
                pdu.get("prev_state_id"), pdu.get("prev_state_origin")
            )

            if prev_state_name in node_map:
                state_edge = pydot.Edge(
                    node_map[start_name], node_map[prev_state_name], style="dotted"
                )
                graph.add_edge(state_edge)

    graph.write("%s.dot" % filename_prefix, format="raw", prog="dot")
    #    graph.write_png("%s.png" % filename_prefix, prog='dot')
    graph.write_svg("%s.svg" % filename_prefix, prog="dot")


def get_pdus(host: str, room: str) -> List[dict]:
    transaction = json.loads(
        urllib.request.urlopen(
            f"http://{host}/_matrix/federation/v1/context/{room}/"
        ).read()
    )

    return transaction["pdus"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate a PDU graph for a given room by talking "
        "to the given homeserver to get the list of PDUs. \n"
        "Requires pydot."
    )
    parser.add_argument(
        "-p", "--prefix", dest="prefix", help="String to prefix output files with"
    )
    parser.add_argument("host")
    parser.add_argument("room")

    args = parser.parse_args()

    host = args.host
    room = args.room
    prefix = args.prefix if args.prefix else "%s_graph" % (room)

    pdus = get_pdus(host, room)

    make_graph(pdus, prefix)
