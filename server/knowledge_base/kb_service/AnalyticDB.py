from __future__ import annotations

import sys

from langchain.vectorstores.analyticdb import Base
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Type
from sqlalchemy import Column, String, Table, create_engine, insert, text, select, Integer, func, and_
from sqlalchemy.dialects.postgresql import ARRAY, JSON, TEXT, REAL, JSONB

from langchain.docstore.document import Document
from langchain.embeddings.base import Embeddings
from langchain.utils import get_from_dict_or_env
from langchain.vectorstores.base import VectorStore

from configs.model_config import *
from server.knowledge_base.kb_service.utils import get_filename_from_source, generate_doc_with_score, merge_ids
from text_splitter.markdown_splitter import md_headers

import uuid

try:
    from sqlalchemy.orm import declarative_base
except ImportError:
    from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()  # type: Any

DEFAULT_EMBEDDING_DIM = 768
DEFAULT_KNOWLEDGE_NAME = "langchain_document"
DEFAULT_KNOWLEDGE_BASE_TABLE_NAME = "knowledge_base"
DEFAULT_FILE_TABLE_NAME = "knowledge_file"


class AnalyticDB(VectorStore):
    """
    VectorStore implementation using AnalyticDB.
    AnalyticDB is a distributed full PostgresSQL syntax cloud-native database.
    - `connection_string` is a postgres connection string.
    - `embedding_function` any embedding function implementing
        `langchain.embeddings.base.Embeddings` interface.
    - `knowledge_name` is the name of the collection to use. (default: langchain_document)
        - NOTE: This is not the name of the table, but the name of the collection.
            The tables will be created when initializing the store (if not exists)
            So, make sure the user has the right permissions to create tables.
    - `pre_delete_collection` if True, will delete the collection if it exists.
        (default: False)
        - Useful for testing.
    """

    def __init__(
            self,
            connection_string: str,
            embedding_function: Embeddings,
            embedding_dimension: int = DEFAULT_EMBEDDING_DIM,
            pre_delete_collection: bool = False,
            logger: Optional[logging.Logger] = None,
            engine_args: Optional[dict] = None,
    ) -> None:
        self.connection_string = connection_string
        self.embedding_function = embedding_function
        self.embedding_dimension = embedding_dimension

        self.pre_delete_collection = pre_delete_collection
        self.logger = logger or logging.getLogger(__name__)

        self.__collection_name = None
        self.__collection_table = None
        self.__base = Base

        self.score_threshold = SCORE_THRESHOLD  # todo 支持0～1的threshold
        self.chunk_content = True
        self.chunk_size = CONTENT_SIZE

        self.__post_init__(engine_args)

    def __del__(self):
        self.__base.metadata.clear()

    def __post_init__(
            self,
            engine_args: Optional[dict] = None,
    ) -> None:
        """
        Initialize the store.
        """

        _engine_args = engine_args or {}

        if (
                "pool_recycle" not in _engine_args
        ):  # Check if pool_recycle is not in _engine_args
            _engine_args[
                "pool_recycle"
            ] = 3600  # Set pool_recycle to 3600s if not present

        self.engine = create_engine(self.connection_string, **_engine_args)
        self.init_collection()

    def init_collection(self) -> None:
        if self.pre_delete_collection:
            self.delete_collection()

        # 初始化MyAnalyticDB不创建collection的Table和绑定self.collection_table，由用户调用接口create_table创建，或者set_collection_name时创建
        # self.collection_table, table_is_exist = self.create_table_if_not_exists()

    def set_embedding(self, embedding_function: Embeddings):
        self.embedding_function = embedding_function

    def get_collection_name(self) -> str:
        return self.__collection_name

    def create_table_if_not_exists(self, collection_name: str = None) -> [Table, bool]:
        """ 返回创建的Table对象和bool类型的table_is_exist，table_is_exist用于判断创建的表是否存在 """
        if collection_name is None:
            collection_name = self.__collection_name
        if collection_name == DEFAULT_KNOWLEDGE_BASE_TABLE_NAME:
            raise Exception(f"知识库名不能和统计知识库的表名{DEFAULT_KNOWLEDGE_BASE_TABLE_NAME}相同")
        if collection_name == DEFAULT_FILE_TABLE_NAME:
            raise Exception(f"知识库名不能和统计文件的表名{DEFAULT_FILE_TABLE_NAME}相同")
        # Define the dynamic collection embedding table
        collection_table = Table(
            collection_name,
            self.__base.metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("uid", TEXT, default=uuid.uuid4),
            Column("embedding", ARRAY(REAL)),
            Column("document", String, nullable=True),
            Column("metadata", JSONB, nullable=True),
            Column("filename", TEXT, nullable=True),  # 存的是filename
            Column("url", TEXT, nullable=True),
            extend_existing=True,
        )
        table_is_exist = True
        with self.engine.connect() as conn:
            with conn.begin():
                # Create the table
                collection_table.create(conn, checkfirst=True)

                # Add the collection in collections set if it doesn't exist
                table_is_exist = False

                # Check if the index exists
                index_name = f"{collection_name}_embedding_idx"
                index_query = text(
                    f"""
                     SELECT 1
                     FROM pg_indexes
                     WHERE indexname = '{index_name}';
                 """
                )
                result = conn.execute(index_query).scalar()

                # Create the index if it doesn't exist
                if not result:
                    index_statement = text(
                        f"""
                         CREATE INDEX "{index_name}"
                         ON "{collection_name}" USING ann(embedding)
                         WITH (
                             "dim" = {self.embedding_dimension},
                             "hnsw_m" = 100
                         );
                     """
                    )
                    conn.execute(index_statement)

        return collection_table, table_is_exist

    def delete_collection(self) -> None:
        if self.__collection_table is None:
            raise Exception("尚未绑定知识库")
        self.logger.debug("Trying to delete knowledge")

        with self.engine.connect() as conn:
            with conn.begin():
                self.__collection_table.drop(conn, checkfirst=True)
                self.__base.metadata.remove(self.__collection_table)
                self.__collection_name = None
                self.__collection_table = None

    def set_collection_name(self, collection_name):
        if self.__collection_table is not None:
            self.__base.metadata.remove(self.__collection_table)
        self.__collection_name = collection_name
        self.__collection_table, table_is_exist = self.create_table_if_not_exists()

    def update_url(self, filename, url) -> None:
        with self.engine.connect() as conn:
            with conn.begin():
                print(url)
                update_metadata = self.__collection_table.update().values(
                    url=url).where(self.__collection_table.c.filename == filename)
                conn.execute(update_metadata)

    def delete(self, ids: Optional[List[str]] = None, **kwargs: Any) -> Optional[bool]:
        """Delete by vector IDs.
        Args:
            ids: List of ids to delete.
        """
        if self.__collection_table is None:
            raise Exception("尚未绑定知识库")
        if ids is None:
            raise ValueError("No ids provided to delete.")

        try:
            with self.engine.connect() as conn:
                with conn.begin():
                    delete_condition = self.__collection_table.c.id.in_(ids)
                    conn.execute(self.__collection_table.delete().where(delete_condition))
                    return True
        except Exception as e:
            print("Delete operation failed:", str(e))
            return False

    def delete_doc(self, source: str or List[str]):
        if self.__collection_table is None:
            raise Exception("尚未绑定知识库")
        try:
            results = []
            # 查出文件路径等于给定的source的记录的id
            with self.engine.connect() as conn:
                with conn.begin():
                    if isinstance(source, str):
                        select_condition = self.__collection_table.c.filename == get_filename_from_source(source)
                        # select_condition = self.collection_table.c.metadata.op("->>")("source") == source
                        s = select(self.__collection_table.c.id).where(select_condition)
                        results = conn.execute(s).fetchall()
                    else:
                        for src in source:
                            select_condition = self.__collection_table.c.filename == get_filename_from_source(src)
                            # select_condition = self.collection_table.c.metadata.op("->>")("source") == src
                            s = select(self.__collection_table.c.id).where(select_condition)
                            results.extend(conn.execute(s).fetchall())

            ids = [result.id for result in results]
            if len(ids) == 0:
                return f"docs delete fail"
            else:
                if self.delete(ids):
                    return f"docs delete success"
                else:
                    return f"docs delete fail"

        except Exception as e:
            print("Delete Doc operation failed:", str(e))
            return f"docs delete fail"

    def update_doc(self, source, new_docs):
        try:
            delete_len = self.delete_doc(source)
            ls = self.add_documents(new_docs)
            return f"docs update success"
        except Exception as e:
            print(e)
            return f"docs update fail"

    def list_docs(self):
        if self.__collection_table is None:
            raise Exception("尚未绑定知识库")
        with self.engine.connect() as conn:
            with conn.begin():
                s = select(self.__collection_table.c.filename).group_by(self.__collection_table.c.filename)
                results = conn.execute(s).fetchall()
        return list(result[0] for result in results)

    def add_texts(
            self,
            texts: Iterable[str],
            metadatas: Optional[List[dict]] = None,
            ids: Optional[List[str]] = None,
            batch_size: int = 500,
            **kwargs: Any,
    ) -> List[str]:
        if self.__collection_table is None:
            raise Exception("尚未绑定知识库")

        if ids is None:
            ids = [str(uuid.uuid1()) for _ in texts]

        embeddings = self.embedding_function.embed_documents(list(texts))

        if not metadatas:
            metadatas = [{} for _ in texts]

        # 导入的文件metadata必须要有source，才可以显示文件的filename和根据filename删除文件
        try:
            filenames = [get_filename_from_source(metadata["source"]) for metadata in metadatas]
        except KeyError:
            raise KeyError("导入的文本没有source，请检查load_file调用的textsplitter")

        print("插入向量总数", len(embeddings))
        cnt = 0
        chunks_table_data = []
        with self.engine.connect() as conn:
            with conn.begin():
                for document, metadata, chunk_id, embedding, filename in zip(
                        texts, metadatas, ids, embeddings, filenames
                ):
                    chunk_table_data = {
                        "uid": chunk_id,
                        "embedding": embedding,
                        "document": document,
                        "metadata": metadata,
                        "filename": filename,
                    }
                    if "url" in metadata.keys():
                        chunk_table_data["url"] = metadata["url"]
                    chunks_table_data.append(chunk_table_data)

                    # Execute the batch insert when the batch size is reached
                    if len(chunks_table_data) == batch_size:
                        conn.execute(insert(self.__collection_table).values(chunks_table_data))
                        # Clear the chunks_table_data list for the next batch
                        chunks_table_data.clear()
                        cnt += 1
                        print(f"已经插入 {batch_size * cnt} 条向量")

                # Insert any remaining records that didn't make up a full batch
                if chunks_table_data:
                    conn.execute(insert(self.__collection_table).values(chunks_table_data))

        return ids

    def similarity_search(
            self,
            query: str,
            k: int = 4,
            filter: Optional[dict] = None,
            **kwargs: Any,
    ) -> List[Document]:
        embedding = self.embedding_function.embed_query(text=query)
        return self.similarity_search_by_vector(
            embedding=embedding,
            k=k,
            filter=filter,
        )

    def similarity_search_with_score(
            self,
            query: str,
            k: int = 4,
            filter: Optional[dict] = None,
    ) -> List[Tuple[Document, float]]:
        embedding = self.embedding_function.embed_query(query)
        if self.chunk_content:  # 使用上下文
            docs_with_scores = self.my_similarity_search_with_score_by_vector_context(
                embedding=embedding, k=k, filter=filter
            )
        else:
            docs_with_scores = self.similarity_search_with_score_by_vector(
                embedding=embedding, k=k, filter=filter
            )
        return docs_with_scores

    def get_search_result_from_database(self,
                                        embedding: List[float],
                                        k: int = 4,
                                        filter: Optional[dict] = None,
                                        ):
        try:
            from sqlalchemy.engine import Row
        except ImportError:
            raise ImportError(
                "Could not import Row from sqlalchemy.engine. "
                "Please 'pip install sqlalchemy>=1.4'."
            )
            # Add the filter if provided
        filter_condition = ""
        if filter is not None:
            conditions = [
                f"metadata->>{key!r} = {value!r}" for key, value in filter.items()
            ]
            filter_condition = f"WHERE {' AND '.join(conditions)}"

        # Define the base query
        sql_query = f"""
                SELECT *, l2_distance(embedding, :embedding) as distance
                FROM {self.__collection_name}
                {filter_condition}
                ORDER BY embedding <-> :embedding
                LIMIT :k
            """

        # Set up the query parameters
        params = {"embedding": embedding, "k": k}

        # Execute the query and fetch the results
        with self.engine.connect() as conn:
            results: Sequence[Row] = conn.execute(text(sql_query), params).fetchall()
        return results

    def similarity_search_by_vector(
            self,
            embedding: List[float],
            k: int = 4,
            filter: Optional[dict] = None,
            **kwargs: Any,
    ) -> List[Document]:
        if self.__collection_table is None:
            raise Exception("尚未绑定知识库")
        if self.chunk_content:  # 使用上下文
            docs_and_scores = self.my_similarity_search_with_score_by_vector_context(
                embedding=embedding, k=k, filter=filter
            )
        else:
            docs_and_scores = self.similarity_search_with_score_by_vector(
                embedding=embedding, k=k, filter=filter
            )
        return [doc for doc, _ in docs_and_scores]

    def similarity_search_with_score_by_vector(
            self,
            embedding: List[float],
            k: int = 4,
            filter: Optional[dict] = None,
    ) -> List[Tuple[Document, float]]:
        """
        不带上下文的相似性搜索
        """
        if self.__collection_table is None:
            raise Exception("尚未绑定知识库")
        results = self.get_search_result_from_database(embedding, k, filter)

        documents_with_scores = []
        for result in results:
            result.metadata["content"] = result.document
            if result.url:
                result.metadata["url"] = result.url
            documents_with_scores.append((
                Document(
                    page_content=result.document,
                    metadata=result.metadata,
                ),
                result.distance
            ))
        return documents_with_scores

    def my_similarity_search_with_score_by_vector_context(
            self,
            embedding: List[float],
            k: int = 4,
            filter: Optional[dict] = None,
    ) -> List[Tuple[Document, float]]:
        """
        带上下文的相似性搜索
        """
        if self.__collection_table is None:
            raise Exception("尚未绑定知识库")

        # Execute the query and fetch the results
        results = self.get_search_result_from_database(embedding, k, filter)

        with self.engine.connect() as conn:
            with conn.begin():
                max_id = conn.execute(select(func.max(self.__collection_table.c.id))).scalar()  # 获得id最大最小值，以确定区间范围
                min_id = conn.execute(select(func.min(self.__collection_table.c.id))).scalar()
        if max_id is None:
            max_id = 0
        if min_id is None:
            min_id = 0

        id_set = set()
        id_map = {}
        batch_size = 20  # 区间一次拓宽多少

        for result in results:
            # count = 0
            # print("查询result", len(result.document), result)

            id_set.add(result.id)
            id_map[result.id] = result
            docs_len = len(result.document)

            # 上下文拼接
            last_l = result.id - 1  # 上一次搜索区间范围上界的前一个
            last_r = result.id + 1  # 上一次搜索区间范围下界的下一个
            for width in range(10, max_id + batch_size, batch_size):  # width是区间宽度/2，从10开始，一次向前后分别拓宽batch_size个
                if last_l < min_id and last_r > max_id:  # 区间已经拓展到id范围外
                    # print("区间已经拓展到id范围外")
                    break

                # print(f"result.id {result.id}, width {width}, range {[result.id - width, result.id + width]}")

                left_range = [result.id - width, last_l]
                right_range = [last_r, result.id + width]

                with self.engine.connect() as conn:  # 查询出上下文
                    with conn.begin():
                        dis_condition = text(f"l2_distance(embedding, :embedding) as distance")
                        file_source_condition = self.__collection_table.c.filename == get_filename_from_source(
                            result.metadata["source"])

                        min_id_condition = self.__collection_table.c.id >= left_range[0]
                        max_id_condition = self.__collection_table.c.id <= left_range[1]
                        s = select(self.__collection_table, dis_condition). \
                            where(and_(min_id_condition, max_id_condition)). \
                            order_by(self.__collection_table.c.id.desc())
                        left_results = conn.execute(s, {"embedding": embedding}).fetchall()

                        min_id_condition = self.__collection_table.c.id >= right_range[0]
                        max_id_condition = self.__collection_table.c.id <= right_range[1]
                        s = select(self.__collection_table, dis_condition). \
                            where(and_(min_id_condition, max_id_condition)). \
                            order_by(self.__collection_table.c.id)
                        right_results = conn.execute(s, {"embedding": embedding}).fetchall()
                        # count += 1

                # print("left", left_range[0], left_range[1])
                # for lid, l_result in enumerate(left_results):
                #     print(lid, len(l_result.document), "(", l_result.id, [l_result.document], ")")
                # print("right", right_range[0], right_range[1])
                # for rid, r_result in enumerate(right_results):
                #     print(rid, len(r_result.document), "(", r_result.id, [r_result.document], ")")

                i = j = 0  # i,j = sys.maxsize表示该方向不再可拼
                if len(left_results) == 0:  # 不存在上文
                    i = sys.maxsize
                if len(right_results) == 0:  # 不存在下文
                    j = sys.maxsize
                while i < len(left_results) or j < len(right_results):
                    if i >= len(left_results):  # 无可拼上文，选择拼下文
                        t_result = right_results[j]
                        j += 1
                        is_left = False
                    elif j >= len(right_results):  # 无可拼下文，选择拼上文
                        t_result = left_results[i]
                        i += 1
                        is_left = True
                    else:
                        if right_results[j].distance <= left_results[i].distance:  # 优先拼距离近的上下文，距离相同拼下文
                            t_result = right_results[j]
                            j += 1
                            is_left = False
                        else:
                            t_result = left_results[i]
                            i += 1
                            is_left = True

                    # 拼上该方向的文本超长度了，或不是同个文件，这个方向不再拼
                    if docs_len + len(t_result.document) > self.chunk_size or \
                            t_result.filename != result.filename:
                        if is_left:
                            i = sys.maxsize
                        else:
                            j = sys.maxsize
                        continue
                    if t_result.filename.lower().endswith(".md"):  # 是markdown
                        is_continue = False
                        for h in range(len(md_headers) - 1, -1, -1):  # 5 到 0
                            header = md_headers[h][1]
                            if (header in t_result.metadata.keys() and header in result.metadata.keys()
                                and t_result.metadata[header] != result.metadata[header]) \
                                    or header in result.metadata.keys() and header not in t_result.metadata.keys():  # 只拼同级且标题相同的
                                if is_left:
                                    i = sys.maxsize
                                else:
                                    j = sys.maxsize
                                is_continue = True
                                break
                        if is_continue:  # 标题不同，跳过后面拼接部分
                            continue

                    if t_result.id in id_set:  # 重叠部分跳过，防止都召回相同的内容，信息量过少
                        continue

                    # 拼接，将id加入id_set
                    docs_len += len(t_result.document)
                    id_set.add(t_result.id)
                    id_map[t_result.id] = t_result
                # print(id_set, docs_len, "i:", i, "j:", j)
                if i == sys.maxsize and j == sys.maxsize:  # 两个方向都无法继续拼了，才退出
                    # print("两个方向都无法继续拼了")
                    break

                last_l = result.id - width - 1
                last_r = result.id + width + 1
            # print("查询次数", count)
        # k个答案拼接完成

        if len(id_set) == 0:
            return []

        id_seqs = merge_ids(id_set)  # 连续的id分在一起，成为一个id seq
        documents_with_scores = generate_doc_with_score(id_seqs, id_map)  # 根据id生成文本

        return documents_with_scores

    @classmethod
    def from_texts(
            cls: Type[AnalyticDB],
            texts: List[str],
            embedding: Embeddings,
            metadatas: Optional[List[dict]] = None,
            embedding_dimension: int = DEFAULT_EMBEDDING_DIM,
            collection_name: str = DEFAULT_KNOWLEDGE_NAME,
            ids: Optional[List[str]] = None,
            pre_delete_collection: bool = False,
            engine_args: Optional[dict] = None,
            **kwargs: Any,
    ) -> AnalyticDB:
        """
        Return VectorStore initialized from texts and embeddings.
        Postgres Connection string is required
        Either pass it as a parameter
        or set the PG_CONNECTION_STRING environment variable.
        """

        connection_string = cls.get_connection_string(kwargs)

        store = cls(
            connection_string=connection_string,
            embedding_function=embedding,
            embedding_dimension=embedding_dimension,
            pre_delete_collection=pre_delete_collection,
            engine_args=engine_args,
        )

        store.add_texts(texts=texts, metadatas=metadatas, ids=ids, **kwargs)
        return store

    @classmethod
    def get_connection_string(cls, kwargs: Dict[str, Any]) -> str:
        connection_string: str = get_from_dict_or_env(
            data=kwargs,
            key="connection_string",
            env_key="PG_CONNECTION_STRING",
        )

        if not connection_string:
            raise ValueError(
                "Postgres connection string is required"
                "Either pass it as a parameter"
                "or set the PG_CONNECTION_STRING environment variable."
            )
        return connection_string

    @classmethod
    def connection_string_from_db_params(
            cls,
            driver: str,
            host: str,
            port: int,
            database: str,
            user: str,
            password: str,
    ) -> str:
        """Return connection string from database parameters."""
        return f"postgresql+{driver}://{user}:{password}@{host}:{port}/{database}"