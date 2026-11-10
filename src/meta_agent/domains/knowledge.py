"""
创建日期：2026-09-12
文件功能：统一知识工具入口，只通过 knowledge-search-v1 获取证据。
"""

from meta_agent.domains.remote_knowledge import RemoteKnowledgeAdapter


class KnowledgeAdapter(RemoteKnowledgeAdapter):
    """Keep the shared Agent/legacy entry point without local retrieval or fallback."""
