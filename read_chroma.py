import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def get_chroma_client(persist_directory: str):
  """获取 Chroma 客户端"""
  try:
      import chromadb
  except ImportError:
      print("Error: chromadb 未安装，请运行: pip install chromadb")
      sys.exit(1)

  path = Path(persist_directory)
  if not path.exists():
      print(f"Error: 路径不存在: {persist_directory}")
      sys.exit(1)

  return chromadb.PersistentClient(path=str(path))


def list_collections(client) -> List[str]:
  """列出所有 collections"""
  collections = client.list_collections()
  return [c.name for c in collections]


def get_collection_info(client, collection_name: str) -> Dict[str, Any]:
  """获取 collection 信息"""
  collection = client.get_collection(name=collection_name)
  count = collection.count()
  metadata = collection.metadata or {}
  peek = collection.peek(limit=1)

  return {
      "name": collection_name,
      "count": count,
      "metadata": metadata,
      "sample_keys": peek.get("ids", [])[:5] if peek else [],
  }


def get_all_collections_detail(client) -> Dict[str, Any]:
  """获取所有 collections 的详细信息"""
  collections = client.list_collections()
  details = []

  for collection in collections:
      info = get_collection_info(client, collection.name)
      details.append({
          "name": collection.name,
          "count": info["count"],
          "metadata": info["metadata"],
      })

  return {
      "total_collections": len(details),
      "collections": details,
      "total_documents": sum(d["count"] for d in details),
  }


def get_all_documents(
  client,
  collection_name: str,
  limit: Optional[int] = None,
  offset: int = 0,
  include: Optional[List[str]] = None,
) -> Dict[str, Any]:
  """获取 collection 中的所有文档"""
  collection = client.get_collection(name=collection_name)

  if include is None:
      include = ["documents", "metadatas", "embeddings"]

  total = collection.count()
  actual_limit = limit if limit else total

  result = collection.get(
      limit=actual_limit,
      offset=offset,
      include=include,
  )

  return {
      "collection_name": collection_name,
      "total_count": total,
      "returned_count": len(result.get("ids", [])),
      "ids": result.get("ids", []),
      "documents": result.get("documents", []),
      "metadatas": result.get("metadatas", []),
      "embeddings": result.get("embeddings", []),
  }


def query_collection(
  client,
  collection_name: str,
  query_texts: Optional[List[str]] = None,
  query_embeddings: Optional[List[List[float]]] = None,
  n_results: int = 10,
  where: Optional[Dict] = None,
) -> Dict[str, Any]:
  """查询 collection"""
  collection = client.get_collection(name=collection_name)

  if query_texts is None and query_embeddings is None:
      raise ValueError("必须提供 query_texts 或 query_embeddings")

  result = collection.query(
      query_texts=query_texts,
      query_embeddings=query_embeddings,
      n_results=n_results,
      where=where,
      include=["documents", "metadatas", "distances", "embeddings"],
  )

  return {
      "collection_name": collection_name,
      "query_texts": query_texts,
      "results": result,
  }


def delete_by_ids(client, collection_name: str, ids: List[str]) -> Dict[str, Any]:
  """根据 ID 删除文档"""
  collection = client.get_collection(name=collection_name)
  collection.delete(ids=ids)
  return {"deleted_ids": ids, "status": "success"}


def delete_collection(client, collection_name: str) -> Dict[str, Any]:
  """删除整个 collection"""
  client.delete_collection(name=collection_name)
  return {"deleted_collection": collection_name, "status": "success"}


def format_output(data: Any, output_format: str = "json") -> str:
  """格式化输出"""
  if output_format == "json":
      return json.dumps(data, ensure_ascii=False, indent=2)
  return str(data)


def main():
  parser = argparse.ArgumentParser(
      description="Chroma 向量数据库读取工具（增强版）",
      formatter_class=argparse.RawDescriptionHelpFormatter,
  )

  parser.add_argument(
      "--path", "-p",
      required=True,
      help="Chroma 数据库路径（持久化目录)"
  )
  parser.add_argument(
      "--format", "-f",
      choices=["json", "text"],
      default="json",
      help="输出格式 (默认: json)"
  )

  subparsers = parser.add_subparsers(dest="command", help="命令")

  # list 命令
  subparsers.add_parser("list", help="列出所有 collections 名称")

  # list-detail 命令（新增）
  subparsers.add_parser("list-detail", help="批量查看所有 collections 详情及条数统计")

  # info 命令
  info_parser = subparsers.add_parser("info", help="查看单个 collection 详情")
  info_parser.add_argument("--name", "-n", required=True, help="Collection 名称")

  # get 命令
  get_parser = subparsers.add_parser("get", help="获取 collection 中的文档")
  get_parser.add_argument("--name", "-n", required=True, help="Collection 名称")
  get_parser.add_argument("--limit", "-l", type=int, help="限制返回数量")
  get_parser.add_argument("--offset", type=int, default=0, help="偏移量")
  get_parser.add_argument("--no-embeddings", action="store_true", help="不返回 embeddings")

  # search 命令
  search_parser = subparsers.add_parser("search", help="搜索 collection")
  search_parser.add_argument("--name", "-n", required=True, help="Collection 名称")
  search_parser.add_argument("--query", "-q", required=True, help="查询文本")
  search_parser.add_argument("--top-k", "-k", type=int, default=10, help="返回结果数量")
  search_parser.add_argument("--where", "-w", help="Metadata 过滤条件 (JSON 字符串)")

  # delete 命令
  delete_parser = subparsers.add_parser("delete", help="删除文档或 collection")
  delete_parser.add_argument("--name", "-n", required=True, help="Collection 名称")
  delete_parser.add_argument("--ids", "-i", nargs="+", help="要删除的文档 ID")
  delete_parser.add_argument("--drop", action="store_true", help="删除整个 collection")

  args = parser.parse_args()

  if not args.command:
      parser.print_help()
      sys.exit(1)

  client = get_chroma_client(args.path)

  if args.command == "list":
      collections = list_collections(client)
      result = {"collections": collections, "count": len(collections)}
      print(format_output(result, args.format))

  elif args.command == "list-detail":
      # 新增：批量查看所有 collection 详情
      detail = get_all_collections_detail(client)
      print(format_output(detail, args.format))

  elif args.command == "info":
      info = get_collection_info(client, args.name)
      print(format_output(info, args.format))

  elif args.command == "get":
      include = ["documents", "metadatas"]
      if not args.no_embeddings:
          include.append("embeddings")

      docs = get_all_documents(
          client,
          args.name,
          limit=args.limit,
          offset=args.offset,
          include=include,
      )
      print(format_output(docs, args.format))

  elif args.command == "search":
      where = json.loads(args.where) if args.where else None

      result = query_collection(
          client,
          args.name,
          query_texts=[args.query],
          n_results=args.top_k,
          where=where,
      )
      print(format_output(result, args.format))

  elif args.command == "delete":
      if args.drop:
          result = delete_collection(client, args.name)
      elif args.ids:
          result = delete_by_ids(client, args.name, args.ids)
      else:
          print("Error: 需要指定 --ids 或 --drop")
          sys.exit(1)
      print(format_output(result, args.format))


if __name__ == "__main__":
  main()