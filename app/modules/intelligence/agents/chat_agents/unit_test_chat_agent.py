import json
import logging
from functools import lru_cache
from typing import AsyncGenerator, Dict, List

from langchain.schema import HumanMessage, SystemMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import (
    ChatPromptTemplate,
    HumanMessagePromptTemplate,
    MessagesPlaceholder,
    SystemMessagePromptTemplate,
)
from langchain_core.runnables import RunnableSequence
from sqlalchemy.orm import Session

from app.modules.conversations.message.message_model import MessageType
from app.modules.conversations.message.message_schema import NodeContext
from app.modules.intelligence.agents.agents.unit_test_agent import (
    kickoff_unit_test_agent,
)
from app.modules.intelligence.agents.agents_service import AgentsService
from app.modules.intelligence.memory.chat_history_service import ChatHistoryService
from app.modules.intelligence.prompts.classification_prompts import (
    AgentType,
    ClassificationPrompts,
    ClassificationResponse,
    ClassificationResult,
)
from app.modules.intelligence.prompts.prompt_schema import PromptResponse, PromptType
from app.modules.intelligence.prompts.prompt_service import PromptService
from app.modules.intelligence.tools.kg_based_tools.get_code_from_node_id_tool import (
    GetCodeFromNodeIdTool,
)

logger = logging.getLogger(__name__)


class UnitTestAgent:
    def __init__(self, mini_llm, llm, db: Session):
        self.mini_llm = mini_llm
        self.llm = llm
        self.history_manager = ChatHistoryService(db)
        self.prompt_service = PromptService(db)
        self.agents_service = AgentsService(db)
        self.chain = None
        self.db = db

    @lru_cache(maxsize=2)
    async def _get_prompts(self) -> Dict[PromptType, PromptResponse]:
        prompts = await self.prompt_service.get_prompts_by_agent_id_and_types(
            "UNIT_TEST_AGENT", [PromptType.SYSTEM, PromptType.HUMAN]
        )
        return {prompt.type: prompt for prompt in prompts}

    async def _create_chain(self) -> RunnableSequence:
        prompts = await self._get_prompts()
        system_prompt = prompts.get(PromptType.SYSTEM)
        human_prompt = prompts.get(PromptType.HUMAN)

        if not system_prompt or not human_prompt:
            raise ValueError("Required prompts not found for UNIT_TEST_AGENT")

        prompt_template = ChatPromptTemplate(
            messages=[
                SystemMessagePromptTemplate.from_template(system_prompt.text),
                MessagesPlaceholder(variable_name="history"),
                MessagesPlaceholder(variable_name="tool_results"),
                HumanMessagePromptTemplate.from_template(human_prompt.text),
            ]
        )
        return prompt_template | self.llm

    async def _classify_query(self, query: str, history: List[HumanMessage]):
        prompt = ClassificationPrompts.get_classification_prompt(AgentType.UNIT_TEST)
        inputs = {"query": query, "history": [msg.content for msg in history[-5:]]}

        parser = PydanticOutputParser(pydantic_object=ClassificationResponse)
        prompt_with_parser = ChatPromptTemplate.from_template(
            template=prompt,
            partial_variables={"format_instructions": parser.get_format_instructions()},
        )
        chain = prompt_with_parser | self.llm | parser
        response = await chain.ainvoke(input=inputs)

        return response.classification

    async def run(
        self,
        query: str,
        project_id: str,
        user_id: str,
        conversation_id: str,
        node_ids: List[NodeContext],
    ) -> AsyncGenerator[str, None]:
        try:
            if not self.chain:
                self.chain = await self._create_chain()
            citations = []
            if not node_ids:
                content = "It looks like there is no context selected. Please type @ followed by file or function name to interact with the unit test agent"
                self.history_manager.add_message_chunk(
                    conversation_id,
                    content,
                    MessageType.AI_GENERATED,
                    citations=citations,
                )
                yield json.dumps({"citations": [], "message": content})
                self.history_manager.flush_message_buffer(
                    conversation_id, MessageType.AI_GENERATED
                )
                return

            history = self.history_manager.get_session_history(user_id, conversation_id)
            for node in node_ids:
                history.append(
                    HumanMessage(
                        content=f"{node.name}: {GetCodeFromNodeIdTool(self.db, user_id).run(project_id, node.node_id)}"
                    )
                )
            validated_history = [
                (
                    HumanMessage(content=str(msg))
                    if isinstance(msg, (str, int, float))
                    else msg
                )
                for msg in history
            ]
            classification = await self._classify_query(query, validated_history)

            tool_results = []
            citations = []
            if classification == ClassificationResult.AGENT_REQUIRED:
                test_response = await kickoff_unit_test_agent(
                    query,
                    validated_history,
                    project_id,
                    node_ids,
                    self.db,
                    self.llm,
                    user_id,
                )

                if test_response.pydantic:
                    citations = test_response.pydantic.citations
                    response = test_response.pydantic.response
                else:
                    citations = []
                    response = test_response.raw

                tool_results = [
                    SystemMessage(
                        content=f"Unit testing agent response, this is not visible to user:\n {response}"
                    )
                ]

            inputs = {
                "history": validated_history,
                "tool_results": tool_results,
                "input": query,
            }

            logger.debug(f"Inputs to LLM: {inputs}")
            citations = self.agents_service.format_citations(citations)
            full_response = ""
            async for chunk in self.chain.astream(inputs):
                content = chunk.content if hasattr(chunk, "content") else str(chunk)
                full_response += content
                self.history_manager.add_message_chunk(
                    conversation_id,
                    content,
                    MessageType.AI_GENERATED,
                    citations=citations,
                )
                yield json.dumps(
                    {
                        "citations": citations,
                        "message": content,
                    }
                )

            logger.debug(f"Full LLM response: {full_response}")

            self.history_manager.flush_message_buffer(
                conversation_id, MessageType.AI_GENERATED
            )

        except Exception as e:
            logger.error(f"Error during UnitTestChatAgent run: {str(e)}", exc_info=True)
            yield f"An error occurred: {str(e)}"
