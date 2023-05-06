from langchain.chains import RetrievalQA
from langchain.vectorstores import AnalyticDB
from langchain.embeddings import OpenAIEmbeddings
from langchain.retrievers import ChatGPTPluginRetriever
from langchain import VectorDBQA, OpenAI
from langchain.prompts import PromptTemplate
from langchain.embeddings.huggingface import HuggingFaceEmbeddings
from langchain.vectorstores import FAISS
from langchain.document_loaders import UnstructuredFileLoader
from models.chatglm_llm import ChatGLM
import sentence_transformers
import os
from configs.model_config import *
import datetime
from typing import List
from textsplitter import ChineseTextSplitter

# return top-k text chunk from vector store
VECTOR_SEARCH_TOP_K = 6

# LLM input history length
LLM_HISTORY_LEN = 3


def load_file(filepath):
    if filepath.lower().endswith(".pdf"):
        loader = UnstructuredFileLoader(filepath)
        textsplitter = ChineseTextSplitter(pdf=True)
        docs = loader.load_and_split(textsplitter)
    else:
        loader = UnstructuredFileLoader(filepath, mode="elements")
        textsplitter = ChineseTextSplitter(pdf=False)
        docs = loader.load_and_split(text_splitter=textsplitter)
    return docs


class LocalDocQA:
    llm: object = None
    embeddings: object = None

    def init_cfg(self,
                 embedding_model: str = EMBEDDING_MODEL,
                 embedding_device=EMBEDDING_DEVICE,
                 llm_history_len: int = LLM_HISTORY_LEN,
                 llm_model: str = LLM_MODEL,
                 llm_device=LLM_DEVICE,
                 top_k=VECTOR_SEARCH_TOP_K,
                 use_ptuning_v2: bool = USE_PTUNING_V2
                 ):
        self.llm = OpenAI()
        self.llm.history_len = llm_history_len

        self.embeddings = OpenAIEmbeddings()
        self.top_k = top_k

    def init_knowledge_vector_store(self,
                                    filepath: str or List[str],
                                    vs_path: str or os.PathLike = None):
        loaded_files = []
        if isinstance(filepath, str):
            if not os.path.exists(filepath):
                print("路径不存在")
                return None
            elif os.path.isfile(filepath):
                file = os.path.split(filepath)[-1]
                try:
                    docs = load_file(filepath)
                    print(f"{file} 已成功加载")
                    loaded_files.append(filepath)
                except Exception as e:
                    print(e)
                    print(f"{file} 未能成功加载")
                    return None
            elif os.path.isdir(filepath):
                docs = []
                for file in os.listdir(filepath):
                    fullfilepath = os.path.join(filepath, file)
                    try:
                        docs += load_file(fullfilepath)
                        print(f"{file} 已成功加载")
                        loaded_files.append(fullfilepath)
                    except Exception as e:
                        print(e)
                        print(f"{file} 未能成功加载")
        else:
            docs = []
            for file in filepath:
                try:
                    docs += load_file(file)
                    print(f"{file} 已成功加载")
                    loaded_files.append(file)
                except Exception as e:
                    print(e)
                    print(f"{file} 未能成功加载")

        if vs_path and os.path.isdir(vs_path):
            vector_store = FAISS.load_local(vs_path, self.embeddings)
            vector_store.add_documents(docs)
        else:
            if not vs_path:
                vs_path = f"""{VS_ROOT_PATH}{os.path.splitext(file)[0]}_FAISS_{datetime.datetime.now().strftime("%Y%m%d_%H%M%S")}"""
            vector_store = FAISS.from_documents(docs, self.embeddings)

        vector_store.save_local(vs_path)
        return vs_path if len(docs) > 0 else None, loaded_files

    def get_knowledge_based_answer(self,
                                   query,
                                   vs_path,
                                   chat_history=[], ):
        prompt_template = """基于以下已知信息回答问题。
    如果无法从中得到答案，请说 "根据已知信息无法回答该问题"，不允许在答案中添加编造成分。
    
    已知内容:
    {context}
    
    问题:
    {question}"""
        prompt = PromptTemplate(
            template=prompt_template,
            input_variables=["context", "question"]
        )
        self.llm.history = chat_history
        retriever = ChatGPTPluginRetriever(url="http://127.0.0.1:8000", bearer_token="foo")
        knowledge_chain = RetrievalQA.from_llm(
            llm=self.llm,
            retriever=retriever,
            prompt=prompt
        )
        knowledge_chain.combine_documents_chain.document_prompt = PromptTemplate(
            input_variables=["page_content"], template="{page_content}"
        )

        knowledge_chain.return_source_documents = True

        result = knowledge_chain({"query": query})
        self.llm.history[-1][0] = query
        return result, self.llm.history
