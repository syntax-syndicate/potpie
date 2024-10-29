import logging
from typing import Any, Dict, List, Optional

from langchain_core.tools import StructuredTool, Tool
from neo4j import GraphDatabase
from sqlalchemy.orm import Session

from app.core.config_provider import config_provider


class GetNodeNeighboursFromNodeIdTool:
    """Tool for retrieving neighbors of a specific node in a repository given its node ID."""

    name = "get_node_neighbours_from_node_id"
    description = (
        "Retrieves neighbors of a specific node in a repository given its node ID"
    )

    def __init__(self, sql_db: Session):
        """
        Initialize the tool with a SQL database session.

        Args:
            sql_db (Session): SQLAlchemy database session.
        """
        self.sql_db = sql_db
        self.neo4j_driver = self._create_neo4j_driver()

    def _create_neo4j_driver(self) -> GraphDatabase.driver:
        """Create and return a Neo4j driver instance."""
        neo4j_config = config_provider.get_neo4j_config()
        return GraphDatabase.driver(
            neo4j_config["uri"],
            auth=(neo4j_config["username"], neo4j_config["password"]),
        )

    def run_tool(self, project_id: str, node_ids: List[str]) -> Dict[str, Any]:
        """
        Run the tool to retrieve neighbors of the specified nodes.

        Args:
            project_id (str): Project ID.
            node_ids (List[str]): List of node IDs to retrieve neighbors for. Should contain atleast one node ID.

        Returns:
            Dict[str, Any]: Neighbor data or error message.
        """
        try:
            result_neighbors = self._get_neighbors(project_id, node_ids)
            if not result_neighbors:
                return {
                    "error": f"No neighbors found for node IDs in project '{project_id}'"
                }

            return {"neighbors": result_neighbors}
        except Exception as e:
            logging.exception(f"An unexpected error occurred: {str(e)}")
            return {"error": f"An unexpected error occurred: {str(e)}"}

    async def run(self, project_id: str, node_ids: List[str]) -> Dict[str, Any]:
        """
        Run the tool to retrieve neighbors of the specified nodes.

        Args:
            project_id (str): Project ID.
            node_ids (List[str]): List of node IDs to retrieve neighbors for. Should contain atleast one node ID.

        Returns:
            Dict[str, Any]: Neighbor data or error message.
        """
        return self.run_tool(project_id, node_ids)

    def _get_neighbors(
        self, project_id: str, node_ids: List[str]
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Retrieve neighbors from Neo4j within 2 hops in either direction.

        Returns a list of dictionaries containing node_id, name and docstring for each neighbor.
        """
        query = """
        MATCH (n:NODE)
        WHERE n.repoId = $project_id AND n.node_id IN $node_ids
        CALL {
            WITH n
            MATCH (n)-[*1..1]-(neighbor:NODE)
            WHERE neighbor.repoId = $project_id
            RETURN DISTINCT neighbor.node_id AS node_id,
                   neighbor.name AS name,
                   neighbor.docstring AS docstring
        }
        RETURN COLLECT({
            node_id: node_id,
            name: name,
            docstring: docstring
        }) as neighbors
        """
        with self.neo4j_driver.session() as session:
            result = session.run(query, project_id=project_id, node_ids=node_ids)
            record = result.single()

            if not record:
                return None
            return record["neighbors"]

    def __del__(self):
        """Ensure Neo4j driver is closed when the object is destroyed."""
        if hasattr(self, "neo4j_driver"):
            self.neo4j_driver.close()


def get_node_neighbours_from_node_id_tool(sql_db: Session) -> Tool:
    tool_instance = GetNodeNeighboursFromNodeIdTool(sql_db)
    return StructuredTool.from_function(
        coroutine=tool_instance.run,
        func=tool_instance.run_tool,
        name="Get Node Neighbours From Node ID",
        description="Retrieves inbound and outbound neighbors of a specific node in a repository given its node ID. This is helpful to find which functions are called by a specific function and which functions are calling the specific function. Works best with Pythoon, JS and TS code.",
    )