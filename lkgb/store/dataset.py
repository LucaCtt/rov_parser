from pathlib import Path

from langchain_core.embeddings import Embeddings
from langchain_neo4j.graphs.graph_document import GraphDocument

from lkgb.config import Config
from lkgb.store.driver import Driver

EVENTS_INDEX_NAME = "eventMessageIndex"
LOG_EXAMPLES_URL = "http://example.com/lkgb/logs/examples"
LOG_TESTS_URL = "http://example.com/lkgb/logs/examples"


class TestEvent:
    def __init__(self, event: str, context: dict, ground_truth: GraphDocument) -> "TestEvent":
        self.event = event
        self.context = context
        self.ground_truth = ground_truth


class Dataset:
    def __init__(self, driver: Driver, embeddings: Embeddings) -> "Dataset":
        self.driver = driver
        self.embeddings = embeddings

    def initialize(self, config: Config) -> None:
        # Load the examples
        self.driver.query(
            "CALL n10s.rdf.import.inline($examples, 'Turtle')",
            params={"examples": Path(config.examples_path).read_text()},
        )

        # Create the vector index
        self.driver.query(
            f"""
            CREATE VECTOR INDEX {EVENTS_INDEX_NAME}
            FOR (n:Event) ON n.embedding
            OPTIONS {{ indexConfig : {{
                `vector.similarity_function` : 'cosine'
            }} }}
            """,
        )

        # Populate the embeddings for the examples
        to_populate = self.driver.query(
            """
            MATCH (n:Event)
            WHERE n.embedding IS null
            RETURN elementId(n) AS id, n.eventMessage as eventMessage
            """,
        )
        text_embeddings = self.embeddings.embed_documents([el["eventMessage"] for el in to_populate])
        self.driver.query(
            """
            UNWIND $data AS row
            MATCH (n:Event)
            WHERE elementId(n) = row.id
            CALL db.create.setNodeVectorProperty(n, 'embedding', row.embedding)
            """,
            params={
                "data": [
                    {"id": el["id"], "embedding": embedding}
                    for el, embedding in zip(to_populate, text_embeddings, strict=True)
                ],
            },
        )

        # Load the tests
        # Note: the test events should not have an embedding
        self.driver.query(
            "CALL n10s.rdf.import.inline($tests, 'Turtle')",
            params={"tests": Path(config.tests_path).read_text()},
        )

    def clear(self) -> None:
        self.driver.query(f"DROP VECTOR INDEX {EVENTS_INDEX_NAME}")
        self.driver.query(
            """
            MATCH (n:Resource)
            WHERE n.uri STARTS WITH $examples_url OR n.uri STARTS WITH $tests_url
            DETACH DELETE n
            """,
            params={"examples_url": LOG_EXAMPLES_URL, "tests_url": LOG_TESTS_URL},
        )

    def tests(self) -> list[TestEvent]:
        test_nodes = self.driver.query(
            """
            MATCH (n:Event)
            WHERE n.uri STARTS WITH $log_tests_url
            RETURN n.eventMessage as message, n.uri as uri
            """,
            params={"log_tests_url": LOG_TESTS_URL},
        )
        tests = []
        for test in test_nodes:
            ground_truth = self.driver.get_subgraph_from_node(test["uri"])

            source_node = next((node for node in ground_truth.nodes if node.type == "Source"), None)
            context = (
                {"source": source_node.properties["sourceName"], "device": source_node.properties["sourceDevice"]}
                if source_node
                else {}
            )
            tests.append(TestEvent(event=test["message"], context=context, ground_truth=ground_truth))

        return tests

    def add_event_graph(self, graph: GraphDocument) -> None:
        """Add an event graph to the store.

        All the nodes will be tagged with the current experiment id,
        and for Event nodes the embedding will be added.

        Args:
            graph (GraphDocument): The event graph to add.

        """
        for node in graph.nodes:
            # Add the experiment_id and (for the Event nodes) the embedding.
            additional_properties = {"experiment_id": self.__config.experiment_id}
            if node.type == "Event":
                # This will raise an exception if the LLM produces an Event node without a message property.
                additional_properties["embedding"] = self.embeddings.embed_query(node.properties["eventMessage"])

            self.driver.query(
                "CALL apoc.create.node([$type], $props) YIELD node",
                params={"type": node.type, "props": {**node.properties, **additional_properties}},
            )

        for relationship in graph.relationships:
            self.driver.query(
                """
                MATCH (a {uri: $source_uri}), (b {uri: $target_uri})
                CALL apoc.create.relationship(a, $type, {}, b) YIELD rel
                RETURN rel
                """,
                params={
                    "source_uri": relationship.source.id,
                    "target_uri": relationship.target.id,
                    "type": relationship.type,
                },
            )

    def search_similar_events(self, event: str, k: int = 5) -> list[GraphDocument]:
        """Search for similar events in the store.

        Args:
            event (str): The event message to search for.
            k (int): The number of similar events to search for.

        Returns:
            list[GraphDocument]: The list of graphs of similar events,
                with the nodes they are connected to and their relationships.

        """
        query_embeddings = self.embeddings.embed_query(event)

        # Find k similar events using embeddings
        similar_events = self.graph_store.query(
            """
            CALL db.index.vector.queryNodes($index, $k, $embedding)
            YIELD node, score
            RETURN node.uri AS node_uri, score
            """,
            params={"index": EVENTS_INDEX_NAME, "k": k, "embedding": query_embeddings},
        )

        return [self.driver.get_subgraph_from_node(similar_event["node_uri"]) for similar_event in similar_events]
