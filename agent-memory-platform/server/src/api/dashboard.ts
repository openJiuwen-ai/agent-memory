/** 仪表盘 API — 2026-07-16 暂用 mock，后续替换为真实后端 */

function delay<T>(data: T, ms = 400): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(data), ms))
}

export function getDashboard(): Promise<any> {
  return delay({
    status: 'healthy',
    total_memories: 50000,
    active_memories: 42000,
    archived_memories: 5000,
    pending_cleanup: 2000,
    retrieval: {
      today_count: 320,
      hit_rate: 0.72,
      hot_words: ['用户偏好', '记忆搜索', '配置查询'],
    },
    storage: {
      usage_mb: 512,
      quota_mb: 1024,
      usage_percent: 50.0,
    },
    tasks: {
      queue_length: 5,
      avg_duration: 2.5,
      success_rate: 0.98,
    },
    llm: {
      embedding_calls: 620,
      llm_calls: 320,
    },
  })
}
