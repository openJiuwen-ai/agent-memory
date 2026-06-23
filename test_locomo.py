import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import logging
import time
import os
from pathlib import Path
import uuid
from filelock import FileLock
from sqlalchemy.ext.asyncio import create_async_engine
from tqdm import tqdm
import sys
from dotenv import load_dotenv
import logging
import sys

# Load environment variables
test_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(test_dir, ".env"))

# Configure logging - output to both console and file
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/app.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
# Import core modules
logger = logging.getLogger(__name__)

# Import core modules
from foundation.llm.model import init_model, Model
from memory_core.manage.mem_model.db_model import create_tables
from common.logging import logger
from foundation.llm.schema.mode_info import ModelConfig, BaseModelInfo
from memory_core.config.config import MemoryEngineConfig
from retrieval.embedding.api_embedding import APIEmbedding
from memory_core.long_term_memory import LongTermMemory
from foundation.store.kv.db_based_kv_store import DbBasedKVStore
from foundation.store.db.default_db_store import DefaultDbStore
from foundation.store.vector.chroma_vector_store import ChromaVectorStore
from foundation.llm.schema.message import AssistantMessage as AIMessage, BaseMessage, UserMessage as HumanMessage
from foundation.store.base_embedding import EmbeddingConfig
from retrieval.common.config import VectorStoreConfig, StoreType
from test_locomo_prompt import validation_prompt, ANSWER_PROMPT

# Configuration items
API_BASE = os.getenv("API_BASE", "mock://api.openai.com/v1")
API_KEY = os.getenv("API_KEY", "sk-fake")
MODEL_NAME = os.getenv("MODEL_NAME", "")
# Configuration items
VALIDATE_API_BASE = os.getenv("VALIDATE_API_BASE", "mock://api.openai.com/v1")
# Global counters
VALIDATE_MODEL_NAME = os.getenv("VALIDATE_MODEL_NAME", "")
VALIDATE_API_KEY = os.getenv("VALIDATE_API_KEY", "sk-fake")
TEMPERATURE = os.getenv("MODEL_TEMPERATURE", 0.95)
os.environ.setdefault("LLM_SSL_VERIFY", "false")
data_path = os.getenv("INPUT_DATA_FILE", "")
if not data_path or not os.path.exists(data_path):
    data_path = os.path.join(test_dir, "locomo10.json")
response_path = os.getenv("RESPONSE_PATH", "./test_response")
result_path = os.getenv("RESULT_PATH", "./test_result.json")
mem_extract_path = os.getenv("MEM_EXTRACT_PATH", "./test_mem_extract")

# Global counters
processed_conversations = 0
progress_bar = None

class ConversationProcessor:
    """Processor for a single conversation (with fully independent LongTermMemory instance)"""
    def __init__(self, conv_id: int):
        self.conv_id = conv_id
        self.memory_engine: LongTermMemory = None  # Private instance
        self.llm_base: Model = None
        self.resource_dir = None
        self.init_done = False
        self.init_lock = asyncio.Lock()
        self.global_chroma_lock = FileLock(f".chroma_init_lock_{conv_id}.lock")
        self.middle_add_time = 0
        self.search_time = 0

    async def init_resources(self):
        """Initialize independent resources for current conversation (key: do not use global singleton)"""
        async with self.init_lock:
            if self.init_done:
                return
            with self.global_chroma_lock:
                # 1. Create independent resource directory
                # project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                project_root = os.path.dirname(os.path.abspath(__file__))
                self.resource_dir = os.path.join(project_root, f'resources_conv_{self.conv_id}')
                os.makedirs(self.resource_dir, exist_ok=True)
                logger.info(f"Conversation {self.conv_id} resource directory: {self.resource_dir}")

                # 2. Initialize embedding model
                embed_config = EmbeddingConfig(
                    base_url=os.getenv("EMBED_API_BASE"),
                    model_name=os.getenv("EMBED_MODEL_NAME"),
                    api_key=os.getenv("EMBED_API_KEY"),
                )
                embed_model = APIEmbedding(
                    config=embed_config,
                    timeout=int(os.getenv("EMBED_TIMEOUT", "60")),
                    max_retries=int(os.getenv("EMBED_MAX_RETRIES", "3")),
                )

                # 3. Create independent storage instances (not registered globally)
                # Chroma vector storage (fixed directory, reuse existing vector data)
                vector_store = ChromaVectorStore(
                    persist_directory=self.resource_dir
                )
                # KV/SQL storage: prioritize reusing existing db with most complete data (ensure cross-run write/read consistency),
                # create new only when not found. Avoid creating empty KV each time leading to missing existing memories.
                import glob as _glob
                import sqlite3 as _sqlite3

                def _fullest(prefix: str) -> str | None:
                    best, best_n = None, -1
                    for fp in _glob.glob(os.path.join(self.resource_dir, prefix + "*.db")):
                        try:
                            if prefix.startswith("kv_store"):
                                conn = _sqlite3.connect(fp)
                                n = conn.execute("SELECT COUNT(*) FROM kv_store").fetchone()[0]
                                conn.close()
                            else:
                                n = os.path.getsize(fp)
                        except Exception:
                            n = -1
                        if n > best_n:
                            best, best_n = fp, n
                    return best if best_n > 0 else None

                kv_existing = _fullest("kv_store_")
                if kv_existing:
                    kv_db_path = Path(kv_existing).resolve()
                    logger.info(f"Conversation {self.conv_id} reusing existing KV: {kv_db_path.name}")
                else:
                    utc_now = datetime.now(timezone.utc)
                    kv_db_path = Path(
                        f"{self.resource_dir}/kv_store_{utc_now.strftime('%Y%m%d%H%M%S')}_"
                        f"{uuid.uuid4().hex[:6]}_{self.conv_id}.db"
                    ).resolve()
                kv_store = DbBasedKVStore(create_async_engine(f"sqlite+aiosqlite:///{kv_db_path}"))

                sql_existing = _fullest("test_sql_db_")
                if sql_existing:
                    db_path = Path(sql_existing).resolve()
                    logger.info(f"Conversation {self.conv_id} reusing existing SQL: {db_path.name}")
                else:
                    utc_now = datetime.now(timezone.utc)
                    db_path = Path(
                        f"{self.resource_dir}/test_sql_db_{utc_now.strftime('%Y%m%d%H%M%S')}_"
                        f"{uuid.uuid4().hex[:6]}_{self.conv_id}.db"
                    ).resolve()
                db_store = DefaultDbStore(create_async_engine(f"sqlite+aiosqlite:///{db_path}"))
                
                # 4. Manually create LongTermMemory instance (key: do not use global singleton)
                # Reset global state (prevent residue)
                if hasattr(LongTermMemory, '_instance'):
                    LongTermMemory._instance = None
                
                # Create new instance
                self.memory_engine = LongTermMemory()
                # Register storage
                await self.memory_engine.register_store(
                    kv_store=kv_store,
                    vector_store=vector_store,
                    db_store=db_store,
                    embedding_model=embed_model
                )
                # Set configuration
                self.memory_engine.set_config(MemoryEngineConfig())

                # 5. Set LLM configuration (bind to current instance)
                from memory_core.config.config import MemoryScopeConfig
                from foundation.llm.schema.config import ModelRequestConfig, ModelClientConfig
                
                scope_config = MemoryScopeConfig(
                    model_cfg=ModelRequestConfig(
                        model=MODEL_NAME,
                        temperature=float(TEMPERATURE),
                    ),
                    model_client_cfg=ModelClientConfig(
                        client_provider="openai",
                        api_key=API_KEY,
                        api_base=API_BASE,
                        verify_ssl=False,
                    ),
                    embedding_cfg=embed_config,
                )
                await self.memory_engine.set_scope_config("default", scope_config)

                # 6. Initialize LLM client
                self.llm_base = init_model(
                    provider="openai",
                    model_name=MODEL_NAME,
                    api_key=API_KEY,
                    api_base=API_BASE,
                    temperature=float(TEMPERATURE),
                    verify_ssl=False,
                )
                
                self.init_done = True
                self.memory_engine.start()
                logger.info(f"Conversation {self.conv_id} resource initialization completed (independent LongTermMemory)")

    async def add_memory(self, user_id: str, app_id: str, messages: list[BaseMessage], timestamp: datetime, session_id: str, retries=5) -> None:
        """Asynchronously add memory (using private LongTermMemory)"""
        from memory_core.config.config import AgentMemoryConfig
        from common.exception.errors import ValidationError
        
        for retry in range(retries):
            try:
                # Use current instance memory_engine directly, not global
                s = time.time()
                add_result = await self.memory_engine.add_messages(
                    messages=messages,
                    agent_config=AgentMemoryConfig(),
                    user_id=user_id,
                    scope_id=app_id,
                    timestamp=timestamp,
                    session_id=session_id
                )
                e = time.time()
                self.middle_add_time += e - s
                print("middle_add_time", self.middle_add_time)
                # await self.memory_engine.middle_mem_to_long(
                #     agent_config=AgentMemoryConfig(),
                #     user_id=user_id,
                #     scope_id=app_id,
                # )
                # Write extracted memory list (AddMemResult) to file for later analysis
                try:
                    self._dump_extracted_memories(add_result, user_id, app_id, session_id, timestamp)
                except Exception as dump_err:
                    logger.warning(f"Conversation {self.conv_id} failed to write extracted memory file: {dump_err}")
                logger.debug(f"Conversation {self.conv_id} successfully added {len(messages)} memories")
                break
            except ValidationError as e:
                if "empty while embedding" in str(e):
                    logger.warning(f"Conversation {self.conv_id} skipping empty embedding, continuing")
                    break
                elif retry < retries - 1:
                    sleep_time = 30 * (retry + 1)
                    await asyncio.sleep(sleep_time)
                    logger.warning(f"Conversation {self.conv_id} add memory failed, retry {retry+1}/{retries}: {str(e)}")
                    continue
                else:
                    logger.error(f"Conversation {self.conv_id} add memory failed: {str(e)}")
                    raise e
            except Exception as e:
                if "TimeoutError" in str(type(e).__name__) or "timeout" in str(e).lower():
                    if retry < retries - 1:
                        sleep_time = 60 * (retry + 1)
                        await asyncio.sleep(sleep_time)
                        logger.warning(f"Conversation {self.conv_id} add memory timeout, retry {retry+1}/{retries} after {sleep_time}s")
                        continue
                    else:
                        logger.error(f"Conversation {self.conv_id} add memory timeout after {retries} retries: {str(e)}")
                        raise e
                elif retry < retries - 1:
                    sleep_time = 30 * (retry + 1)
                    await asyncio.sleep(sleep_time)
                    logger.warning(f"Conversation {self.conv_id} add memory failed, retry {retry+1}/{retries} after {sleep_time}s: {str(e)}")
                    continue
                else:
                    logger.error(f"Conversation {self.conv_id} add memory failed: {str(e)}")
                    raise e

    def _dump_extracted_memories(self, add_result, user_id: str, app_id: str, session_id: str, timestamp) -> None:
        """Serialize the extracted memory list (AddMemResult) from add_messages to JSONL file, separate files by conv_id."""
        from dataclasses import asdict
        from enum import Enum

        def _ser(unit):
            d = asdict(unit)
            return {k: (v.value if isinstance(v, Enum) else v) for k, v in d.items()}

        record = {
            "conv_id": self.conv_id,
            "session_id": session_id,
            "user_id": user_id,
            "scope_id": app_id,
            "timestamp": str(timestamp),
            "counts": {
                "variables": len(add_result.variables),
                "user_profile": len(add_result.user_profile),
                "semantic_memory": len(add_result.semantic_memory),
                "episodic_memory": len(add_result.episodic_memory),
                "summary": len(add_result.summary),
            },
            "memories": {
                "variables": [_ser(u) for u in add_result.variables],
                "user_profile": [_ser(u) for u in add_result.user_profile],
                "semantic_memory": [_ser(u) for u in add_result.semantic_memory],
                "episodic_memory": [_ser(u) for u in add_result.episodic_memory],
                "summary": [_ser(u) for u in add_result.summary],
            },
        }
        path = f"{mem_extract_path}{self.conv_id}.jsonl"
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
        logger.debug(
            f"Conversation {self.conv_id} extracted memories written to {path} "
            f"(variables={record['counts']['variables']}, user_profile={record['counts']['user_profile']}, "
            f"semantic={record['counts']['semantic_memory']}, episodic={record['counts']['episodic_memory']}, "
            f"summary={record['counts']['summary']})"
        )

    async def process_locomo_data(self, speaker_a: str, speaker_b: str, data: dict):
        """Asynchronously process conversation data (write to private directory)"""
        conversation_data = data['conversation']
        user_id = "default"
        app_id = "default"

        for key in conversation_data.keys():
            messages = []
            if key in ["speaker_a", "speaker_b"] or "date" in key or "timestamp" in key:
                continue
            
            session_id = f"{self.conv_id}-{str(key).replace('_', '-')}"
            data_time_key = key + "_date_time"
            timestamp = conversation_data[data_time_key]
            timestamp = datetime.strptime(timestamp, "%I:%M %p on %d %B, %Y")
            chats = conversation_data[key]

            for chat in chats:
                message = f"{chat['speaker']}:{chat['text']}"
                if chat.get('blip_caption'):
                    message += f"(The conversation is accompanied by an image, and the description of the image is '{chat['blip_caption']}')"
                message = message
                
                if not message.strip():
                    continue
                
                if chat['speaker'] == speaker_a:
                    message = HumanMessage(content=message, name=chat['speaker'])
                elif chat['speaker'] == speaker_b:
                    message = AIMessage(content=message, name=chat['speaker'])
                
                messages.append(message)
                
                if len(messages) == 2:
                    await self.add_memory(user_id, app_id, messages, timestamp, session_id)
                    messages = []
                    timestamp += timedelta(seconds=1)
            
            if messages:
                await self.add_memory(user_id, app_id, messages, timestamp, session_id)

    async def llm_answer(self, user_id: str, app_id: str, query: str, per_type_num: int = 3) -> str:
        """异步调用LLM生成回答（读取私有目录数据）。

        检索策略：按 mem_type（user_profile / semantic_memory / episodic_memory）各召回
        per_type_num 条，合并去重。避免 semantic/user_profile 因分数高而把含时间的
        episodic 记忆挤出 top，导致时间类问题答不出。
        """
        from memory_core.manage.search.search_manager import SearchParams

        await self.memory_engine._apply_scope_embedding(app_id)
        merged: dict[str, dict] = {}
        for mem_type in ["user_profile", "semantic_memory", "episodic_memory"]:
            try:
                params = SearchParams(
                    query=query, scope_id=app_id, top_k=per_type_num,
                    user_id=user_id, threshold=0.0, search_type=[mem_type],
                )
                items = await self.memory_engine.search_manager.search(params)
                params.search_type = None
                middle_items = await self.memory_engine.search_user_mem(query=query,
                            num=10,
                            user_id=user_id,
                            scope_id=app_id,
                            threshold=0.0)
                print("items",items)
                print("middle_items", middle_items)
                # exit(1)
            except Exception as e:
                logger.warning(f" Conversation {self.conv_id} 检索 {mem_type} 失败: {e}")
                items = []
                middle_items = []
            for item in middle_items:
                mem_id, score, content, timestamp = item
                it = {
                    "id": mem_id,
                    "mem": content,
                    "score": score,
                    "mem_type": "middle_term_memory",
                    "timestamp": timestamp
                }
                mid = it.get("id")
                if mid is None or mid not in merged:
                    merged[mid] = it
                elif (it.get("score", 0.0) or 0.0) > (merged[mid].get("score", 0.0) or 0.0):
                    merged[mid] = it

            for it in items:
                mid = it.get("id")
                if mid is None or mid not in merged:
                    merged[mid] = it
                elif (it.get("score", 0.0) or 0.0) > (merged[mid].get("score", 0.0) or 0.0):
                    merged[mid] = it

        memory_msg = ""
        for it in merged.values():
            ts = it.get("timestamp") or ""
            memory_msg += f"${ts}: ${it.get('mem', '')}\n"
        llm_prompt = ANSWER_PROMPT.substitute(question=query, memory=memory_msg)
        print("llm_prompt", llm_prompt)
        # exit(1)
        logger.debug(f" Conversation {self.conv_id} LLM Prompt: {llm_prompt[:100]}...")

        message = HumanMessage(content=llm_prompt)
        response = await self.llm_base.invoke(messages=[message], model=MODEL_NAME)
        return response.content

    async def generate_response(self, qa_data: list, user_name1: str, user_name2: str):
        """异步生成QA响应"""
        response_path_qa = f"{response_path}{self.conv_id}.json"
        # 清空文件（避免追加旧数据）
        open(response_path_qa, 'w', encoding='utf-8').close()

        user_id = "default"
        app_id = "default"

        for idx, qa_enum in enumerate(qa_data):
            category = qa_enum['category']
            if category > 4:
                continue

            # question = qa_enum['question'].replace(user_name1, 'user').replace(user_name2, 'assistant')
            # answer = str(qa_enum['answer']).replace(user_name1, 'user').replace(user_name2, 'assistant')
            question = qa_enum['question']
            answer = str(qa_enum['answer'])

            for retry in range(3):
                try:
                    response = await self.llm_answer(user_id, app_id, question)
                    break
                except Exception as e:
                    if retry < 2:
                        await asyncio.sleep(30)
                        logger.warning(f" Conversation {self.conv_id} QA生成失败，重试 {retry+1}/3: {str(e)}")
                        continue
                    else:
                        logger.error(f" Conversation {self.conv_id} QA生成失败: {str(e)}")
                        raise e

            data_dict = {
                "question": question,
                "answer": answer,
                "response": response,
                "category": category
            }
            with open(response_path_qa, 'a', encoding='utf-8') as file:
                json.dump(data_dict, file, ensure_ascii=False)
                file.write('\n')

    async def llm_service(self, user_message: str) -> AIMessage:
        """异步调用LLM进行验证"""
        from foundation.llm.schema.message import SystemMessage
        messages = [
            SystemMessage(content="You are an expert grader that determines if answers to questions match a gold standard answer"),
            HumanMessage(content=user_message)
        ]
        for retry in range(3):
            try:
                response = await self.llm_base.invoke(messages=messages, model=VALIDATE_MODEL_NAME)
                break
            except Exception as e:
                if retry < 2:
                    await asyncio.sleep(30)
                    logger.warning(f" Conversation {self.conv_id} 判断失败，重试 {retry+1}/3: {str(e)}")
                    continue
                else:
                    logger.error(f" Conversation {self.conv_id} 判断失败: {str(e)}")
                    raise e
        return response

    async def validate_locomo_data(self, speaker_a: str, speaker_b: str):
        """异步验证回答正确性"""
        response_path_enum = f"{response_path}{self.conv_id}.json"
        qa_result_dict = defaultdict(int)
        correct_result_dict = defaultdict(int)

        with open(response_path_enum, 'r', encoding='utf-8') as f:
            for qa_enum_str in f:
                if not qa_enum_str.strip():
                    continue

                qa_enum = json.loads(qa_enum_str)
                # question = qa_enum['question'].replace(speaker_a, 'user').replace(speaker_b, 'assistant')
                # gold_answer = str(qa_enum['answer']).replace(speaker_a, 'user').replace(speaker_b, 'assistant')
                question = qa_enum['question']
                gold_answer = str(qa_enum['answer'])
                category = qa_enum['category']
                response = qa_enum['response']

                qa_result_dict[category] += 1
                if str(response).strip() == "":
                    continue

                test_prompt = validation_prompt.format(
                    question=question,
                    gold_answer=gold_answer,
                    response=response
                )

                result = await self.llm_service(test_prompt)
                logger.info(f" Conversation {self.conv_id} 验证结果: {result.content[:50]}...")

                if "CORRECT" in result.content and "WRONG" not in result.content:
                    correct_result_dict[category] += 1

        accuracy_per_class = {}
        for cls in [1, 2, 3, 4]:
            total = qa_result_dict[cls]
            correct = correct_result_dict[cls]
            accuracy = correct / total if total != 0 else 0.0
            accuracy_per_class[cls] = round(accuracy, 4)

        with open(result_path, 'a', encoding='utf-8') as file:
            total_result = {
                "conversation_id": self.conv_id,
                "accuracy_per_class": accuracy_per_class,
                "qa_result_dict": qa_result_dict,
                "correct_result_dict": correct_result_dict
            }
            json.dump(total_result, file, ensure_ascii=False)
            file.write('\n')

    async def process_full(self, data_enum: dict):
        """处理单个conversation的完整流程"""
        global processed_conversations, progress_bar

        try:
            s = time.time()
            # 1. 初始化独立资源（关键：创建私有LongTermMemory）
            await self.init_resources()

            # 2. 获取speaker信息
            speaker_a = data_enum['conversation']['speaker_a']
            speaker_b = data_enum['conversation']['speaker_b']

            # 3. 处理对话数据（写入私有目录）—— 已注释：复用 resources_conv_* 已写入的记忆，跳过写入
            # logger.info(f" 跳过写入步骤，复用已有记忆（resources_conv_{self.conv_id}）")
            await self.process_locomo_data(speaker_a, speaker_b, data_enum)
            e = time.time()
            print("用户感知的时延", e - s)
            # exit(1)

            await asyncio.sleep(2700)
            # 4. 生成QA响应（读取私有目录数据）
            from memory_core.config.config import MemoryScopeConfig
            from foundation.llm.schema.config import ModelRequestConfig, ModelClientConfig

            scope_config = MemoryScopeConfig(
                model_cfg=ModelRequestConfig(
                    model=VALIDATE_MODEL_NAME,
                    temperature=float(TEMPERATURE),
                ),
                model_client_cfg=ModelClientConfig(
                    client_provider="openai",
                    api_key=VALIDATE_API_KEY,
                    api_base=VALIDATE_API_BASE,
                    verify_ssl=False,
                ),
                embedding_cfg=EmbeddingConfig(
                    base_url=os.getenv("EMBED_API_BASE"),
                    model_name=os.getenv("EMBED_MODEL_NAME"),
                    api_key=os.getenv("EMBED_API_KEY"),
                ),
            )
            await self.memory_engine.set_scope_config("default", scope_config)
            self.llm_base = init_model(
                provider="openai",
                model_name=VALIDATE_MODEL_NAME,
                api_key=VALIDATE_API_KEY,
                api_base=VALIDATE_API_BASE,
                temperature=float(TEMPERATURE),
                verify_ssl=False,
            )
            s_search = time.time()
            await self.generate_response(data_enum['qa'], speaker_a, speaker_b)
            e_search = time.time()
            self.search_time += e_search - s_search
            logger.info(f"search time: {self.search_time}")

            self.memory_engine.stop()

            # 5. 验证结果

            await self.validate_locomo_data(speaker_a, speaker_b)

            # 验证：检查当前目录是否有数据写入
            if os.path.exists(self.resource_dir):
                file_count = len(os.listdir(self.resource_dir))
                logger.info(f" Conversation {self.conv_id} 目录文件数: {file_count}")

            logger.info(f" 完成处理 Conversation {self.conv_id}")

        except Exception as e:
            logger.error(f" Conversation {self.conv_id} 处理失败: {str(e)}", exc_info=True)
            raise
        finally:
            # 更新进度
            processed_conversations += 1
            if progress_bar:
                progress_bar.update(1)

class TESTLOCOMO:
    @staticmethod
    def get_locomo_data(data_path: str):
        """加载数据"""
        with open(data_path, 'r', encoding="utf-8") as f:
            data = json.load(f)
            return data[:1]

    @staticmethod
    def overall_compute(result_path_enum: str) -> None:
        """计算总体结果"""
        key_mapping = {
            "4": "Single-hop",
            "1": "Multi-hop",
            "2": "Temporal reasoning",
            "3": "Open domain"
        }
        total_qa = defaultdict(int)
        total_correct = defaultdict(int)

        with open(result_path_enum, "r", encoding="utf-8") as f:
            for line in f:
                data = json.loads(line.strip())
                if "qa_result_dict" not in data:
                    continue

                for k, v in data["qa_result_dict"].items():
                    total_qa[key_mapping.get(str(k), str(k))] += v
                for k, v in data["correct_result_dict"].items():
                    total_correct[key_mapping.get(str(k), str(k))] += v

        total_accuracy = {}
        total_correct_all = 0
        total_samples = 0

        for key in key_mapping.values():
            correct = total_correct.get(key, 0)
            total = total_qa.get(key, 0)
            total_accuracy[key] = round(correct / total if total != 0 else 0, 4)
            total_correct_all += correct
            total_samples += total

        total_accuracy["overall"] = round(
            total_correct_all / total_samples if total_samples != 0 else 0,
            4
        )

        logger.info(" 总样本量统计：%s", dict(total_qa))
        logger.info(" 总正确量统计：%s", dict(total_correct))
        logger.info(" 总准确率：%s", total_accuracy)

        with open(result_path_enum, "a", encoding="utf-8") as f:
            json.dump({
                "total_samples": dict(total_qa),
                "total_correct": dict(total_correct),
                "Overall": total_accuracy
            }, f, ensure_ascii=False, indent=4)
            f.write('\n')

async def main():
    global progress_bar
    logger.set_level(logging.INFO)

    # 清空结果文件（避免追加旧数据）
    open(result_path, 'w', encoding='utf-8').close()

    # 1. 加载数据
    test = TESTLOCOMO()
    data = test.get_locomo_data(data_path)
    total_convs = len(data)
    logger.info(f" 加载到 {total_convs} 个conversation")

    # 2. 初始化进度条
    progress_bar = tqdm(total=total_convs, desc="处理所有Conversation")

    # 3. 逐个创建处理器并执行（确保实例不被覆盖）
    #    注：这里改为逐个创建+立即执行，而非批量创建后gather，进一步避免单例冲突
    tasks = []
    for conv_id, data_enum in enumerate(data):
        print("conv_id", conv_id)
        print("data_enum", data_enum)
        processor = ConversationProcessor(conv_id)
        await processor.process_full(data_enum)
        # 添加到任务列表
        # task = processor.process_full(data_enum)
        # tasks.append(task)

    # 4. 执行所有任务（协程交替执行）
    # await asyncio.gather(*tasks, return_exceptions=True)
    # await asyncio.gather(*tasks[1:2], return_exceptions=False)
    # await asyncio.gather(*tasks[2:3], return_exceptions=False)
    # await asyncio.gather(*tasks[3:4], return_exceptions=False)
    # await asyncio.gather(*tasks[4:5], return_exceptions=False)
    # await asyncio.gather(*tasks[5:6], return_exceptions=False)
    # await asyncio.gather(*tasks[6:7], return_exceptions=False)
    # await asyncio.gather(*tasks[7:8], return_exceptions=False)
    # await asyncio.gather(*tasks[8:9], return_exceptions=False)
    # await asyncio.gather(*tasks[9:10], return_exceptions=False)
    # 5. 计算总体结果
    test.overall_compute(result_path)

    # 6. 完成
    progress_bar.close()
    logger.info(" 所有Conversation处理完成！")

    logger.info(" 验证：每个conversation的数据已写入各自的resources_conv_*目录")

if __name__ == '__main__':
    # Windows事件循环修复
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    asyncio.run(main())
