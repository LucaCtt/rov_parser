"""Parser module for parsing log events and constructing knowledge graphs."""

import uuid
from enum import Enum
from typing import cast

from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_neo4j.graphs.graph_document import GraphDocument, Node, Relationship
from pydantic import BaseModel, Field

from lkgb.reports import ParserReport
from lkgb.store import Store
from lkgb.tools import fetch_ip_address_info


class _EventGraph(BaseModel):
    nodes: list
    relationships: list


def _similar_message_group(event: str, context: dict, graph: GraphDocument) -> list[BaseMessage]:
    nodes = [
        {
            "id": node.id,
            "type": node.type,
            "properties": [{"type": key, "value": value} for key, value in node.properties.items()],
        }
        for node in graph.nodes
    ]

    relationships = [
        {
            "source_id": rel.source.id,
            "target_id": rel.target.id,
            "type": rel.type,
        }
        for rel in graph.relationships
    ]

    return [
        HumanMessage(f"Event: '{event}'\nContext: {context}", name="example_user"),
        AIMessage(
            "",
            name="example_assistant",
            tool_calls=[
                {
                    "name": "DynamicEventGraph",
                    "args": {
                        "nodes": nodes,
                        "relationships": relationships,
                    },
                    "id": "1",
                },
            ],
        ),
        ToolMessage("", tool_call_id="1"),
    ]


class Parser:
    """The Parser class is responsible for parsing log events and identifying their templates."""

    def __init__(
        self,
        parser_model: BaseChatModel,
        store: Store,
        prompt_build_graph: str,
        self_reflection_steps: int,
    ) -> None:
        self.history = ChatMessageHistory()
        self.store = store
        self.prompt_build_graph = prompt_build_graph
        self.self_reflection_steps = self_reflection_steps

        try:
            parser_model.with_structured_output(_EventGraph)
        except NotImplementedError as e:
            msg = "The parser model must support structured output."
            raise ValueError(msg) from e

        # Add context enrichment tools.
        # Note: not all models support tools + structured output
        structured_model = parser_model.bind_tools([fetch_ip_address_info])

        # Add the graph structure to the structured output.
        # Also include raw output to retrieve eventual errors.
        structured_model = structured_model.with_structured_output(self.__create_graph_structure(), include_raw=True)  # type: ignore[attr-defined]

        gen_graph_prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    self.prompt_build_graph,
                ),
                ("placeholder", "{examples}"),
                ("human", "Event: '{event}'\nContext: '{context}'"),
            ],
        )

        self.chain = gen_graph_prompt | structured_model

    def __create_graph_structure(self) -> type[_EventGraph]:
        ontology = self.store.ontology.graph()

        valid_node_types = [node.type for node in ontology.nodes]
        valid_relationships = [rel.type for rel in ontology.relationships]
        valid_triples = [(rel.source.type, rel.type, rel.target.type) for rel in ontology.relationships]

        valid_properties: list[str] = ["uri"]
        valid_properties_dict: dict[str, list[str]] = {}
        for node in ontology.nodes:
            valid_properties = valid_properties + list(node.properties.keys())
            valid_properties_dict[node.type] = list(node.properties.keys())

        valid_properties_schema = [f"{node}:{props}" for node, props in valid_properties_dict.items()]

        _NodeType = Enum("_NodeType", {node: node for node in valid_node_types}, type=str)  # noqa: N806
        _PropertyType = Enum("_PropertyType", {prop: prop for prop in valid_properties}, type=str)  # noqa: N806
        _RelationshipType = Enum("_RelationshipType", {rel: rel for rel in valid_relationships}, type=str)  # noqa: N806

        class _Property(BaseModel):
            type: _PropertyType = Field(  # type: ignore[valid-type]
                description=f"The type or label of the property. Available options are: {valid_properties}",
            )
            value: str | float = Field(description=("Extracted value."))

        class _Node(BaseModel):
            id: str = Field(description="Name or human-readable unique identifier.")
            type: _NodeType = Field(  # type: ignore[valid-type]
                description=f"The type or label of the node. Available options are: {valid_node_types}",
            )
            properties: list[_Property] | None = Field(default=None, description="List of node properties.")

        _Node.__doc__ = (
            "Each node type has a specific set of available properties. "
            f"The available properties for each node type are: {valid_properties_schema} "
        )

        class _Relationship(BaseModel):
            source_id: str = Field(description="Name or human-readable unique identifier of source node.")
            target_id: str = Field(description="Name or human-readable unique identifier of source node.")
            type: _RelationshipType = Field(  # type: ignore[valid-type]
                description=f"The type or label of the relationship. Available types are: {valid_relationships}",
            )

        _Relationship.__doc__ = (
            "Each relationship type has a specific source and target node type. "
            "The available relationships specified in the format "
            f"(source type, relationship type, target type) are: {valid_triples} "
        )

        class DynamicEventGraph(_EventGraph):
            nodes: list[_Node] = Field(description="List of nodes.")
            relationships: list[_Relationship] = Field(
                description="List of relationships.",
            )

        return DynamicEventGraph

    def _get_similar(self, event: str) -> list[BaseMessage]:
        similar_event_graph = self.store.dataset.search_similar_events(event, k=1)

        # Handle no similar events found, or no nodes in the graph.
        # This should never happen, but if it does, return an empty list.
        if (len(similar_event_graph) == 0) or (len(similar_event_graph[0].nodes) == 0):
            return []

        # The similar event may have no source node,
        # or the node may have no sourceName or sourceDevice properties.
        context = {}

        # This is to differentiate the ids of each example,
        # to prevent the llm from generating the same ids at different parse calls.
        curr_id = str(uuid.uuid4())[:8]

        for node in similar_event_graph[0].nodes:
            if node.type == "Source":
                context["source"] = node.properties.get("sourceName")
                context["device"] = node.properties.get("sourceDevice")

            if isinstance(node.id, str):
                node.id = node.id.replace("examples", f"run/{curr_id}")

            uri = node.properties.get("uri")
            if uri:
                node.properties["uri"] = uri.replace("examples", f"run/{curr_id}")

        return _similar_message_group(event, context, similar_event_graph[0])

    def parse(self, event: str, context: dict) -> ParserReport:
        """Parse the given event and construct a knowledge graph.

        Args:
            event: The log event to parse.
            context: The context of the event.

        Returns:
            A report containing the stats of the parsing process.

        """
        report = ParserReport()

        raw_schema = self.chain.invoke(
            {"event": event, "context": context, "examples": self._get_similar(event)},
            {"configurable": {"session_id": "unused"}},
        )

        raw_schema = cast(dict, raw_schema)

        # Error handling for when the output is not parsed correctly
        if not raw_schema.get("parsed"):
            # If the tool has an exception, return it
            if raw_schema.get("parsing_error"):
                return report.failure(raw_schema["parsing_error"])

            # Otherwise try to return the output message from the llm
            if raw_schema.get("out"):
                # The output message should always be an AIMessage,
                # but if it is not, return the raw output.
                try:
                    raw_out = cast(AIMessage, raw_schema["out"])
                    return report.failure(raw_out.text())
                except ValueError:
                    return report.failure(raw_schema["out"])

            return report.failure("The output was not parsed correctly, and no error message was returned by the llm.")

        # Construct the graph from the structured output.
        try:
            nodes_dict = {
                node.id: Node(
                    id=node.id,
                    type=node.type,
                    properties={prop.type: prop.value for prop in node.properties} if node.properties else {},
                )
                for node in raw_schema["parsed"].nodes
            }
            # The relationships may refer to a node that is not present in the list of nodes.
            # For now, this just fails the parsing process. In the future a number of
            # correction steps could be taken to try to recover from this situation.
            relationships = []
            for rel in raw_schema["parsed"].relationships:
                source_node = nodes_dict[rel.source_id]
                target_node = nodes_dict[rel.target_id]

                relationships.append(Relationship(source=source_node, target=target_node, type=rel.type))
        except KeyError as e:
            return report.failure(e)

        graph = GraphDocument(nodes=list(nodes_dict.values()), relationships=relationships)

        return report.success(graph)
