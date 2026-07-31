/*
 * Copyright 2024 OpenJiuWen
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package com.openjiuwen.memory.opscenter.websocket;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.socket.CloseStatus;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.handler.TextWebSocketHandler;

import java.io.IOException;
import java.time.Instant;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;

/**
 * WebSocket 连接健康监控处理器
 * 负责维护前端与后端的实时连接状态检测
 * 
 * 核心机制：
 * - 建立连接时自动记录会话信息
 * - 定期向所有活跃连接发送心跳信号
 * - 连接关闭时自动清理资源
 * - 异常情况下及时释放会话对象
 */
@Component
public class HeartbeatHandler extends TextWebSocketHandler {
    
    private static final Logger log = LoggerFactory.getLogger(HeartbeatHandler.class);
    /** 心跳间隔（毫秒）- 5 秒一次 */
    private static final long HEARTBEAT_INTERVAL_MS = 5000L;
    /** 会话存储容器 */
    private final ConcurrentHashMap<String, WebSocketSession> sessions = new ConcurrentHashMap<>();
    /** 定时任务调度器 */
    private final ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor();
    /** 定时任务引用 */
    private ScheduledFuture<?> heartbeatTask;
    /** 心跳消息模板 */
    private static final String HEARTBEAT_MESSAGE = "{\"event\":\"heartbeat\",\"ts\":%d}";

    public HeartbeatHandler() {
        // 启动定时任务：每 5 秒向所有连接推送心跳
        scheduler.scheduleAtFixedRate(
            this::broadcastHeartbeat,
            HEARTBEAT_INTERVAL_MS,
            HEARTBEAT_INTERVAL_MS,
            TimeUnit.MILLISECONDS
        );
    }

    @Override
    public void afterConnectionEstablished(WebSocketSession session) throws Exception {
        String sessionId = session.getId();
        sessions.put(sessionId, session);
        log.info("[WebSocket] 前端连接已建立 - sessionId={}, 当前连接数={}", sessionId, sessions.size());
        
        // 立即发送第一次心跳
        sendHeartbeat(session);
    }

    @Override
    public void afterConnectionClosed(WebSocketSession session, CloseStatus status) throws Exception {
        String sessionId = session.getId();
        sessions.remove(sessionId);
        log.info("[WebSocket] 前端连接已关闭 - sessionId={}, status={}, 剩余连接数={}", 
                 sessionId, status, sessions.size());
    }

    @Override
    protected void handleTextMessage(WebSocketSession session, TextMessage message) throws Exception {
        // 可选：处理前端发来的心跳请求（当前方案是后端主动推送，前端可不发送）
        String payload = message.getPayload();
        if ("ping".equalsIgnoreCase(payload)) {
            sendHeartbeat(session);
        }
    }

    /**
     * 向所有连接的客户端广播心跳
     */
    private void broadcastHeartbeat() {
        if (sessions.isEmpty()) {
            return;
        }
        
        String heartbeat = String.format(
            "{\"type\":\"heartbeat\",\"timestamp\":\"%s\",\"connections\":%d}",
            Instant.now().toString(),
            sessions.size()
        );
        
        sessions.forEach((id, session) -> {
            try {
                if (session.isOpen()) {
                    session.sendMessage(new TextMessage(heartbeat));
                } else {
                    sessions.remove(id);
                }
            } catch (IOException e) {
                log.warn("[WebSocket] 发送心跳失败 - sessionId={}, 移除连接", id, e);
                sessions.remove(id);
            }
        });
    }

    /**
     * 向单个会话发送心跳
     */
    private void sendHeartbeat(WebSocketSession session) {
        try {
            if (session.isOpen()) {
                String heartbeat = String.format(
                    "{\"type\":\"heartbeat\",\"timestamp\":\"%s\"}",
                    Instant.now().toString()
                );
                session.sendMessage(new TextMessage(heartbeat));
            }
        } catch (IOException e) {
            log.warn("[WebSocket] 发送心跳失败", e);
        }
    }

    /**
     * 获取当前连接数（用于健康检查接口）
     */
    public int getConnectionCount() {
        return sessions.size();
    }

    /**
     * 清理资源（应用关闭时调用）
     */
    public void destroy() {
        scheduler.shutdown();
        try {
            if (!scheduler.awaitTermination(5, TimeUnit.SECONDS)) {
                scheduler.shutdownNow();
            }
        } catch (InterruptedException e) {
            scheduler.shutdownNow();
            Thread.currentThread().interrupt();
        }
        
        sessions.forEach((id, session) -> {
            try {
                session.close();
            } catch (IOException e) {
                log.warn("[WebSocket] 关闭会话失败 - sessionId={}", id, e);
            }
        });
        sessions.clear();
    }
}
