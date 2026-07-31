package com.openjiuwen.memory.opscenter.websocket;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.socket.CloseStatus;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.handler.TextWebSocketHandler;

import java.io.IOException;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * WebSocket 心跳处理器 - 向前端实时推送后端健康状态。
 * 
 * 工作原理：
 * 1. 前端建立 WebSocket 连接后，后端每 5 秒推送一次心跳包
 * 2. 前端收到心跳包说明后端在线，超过 10 秒未收到说明后端断开
 * 3. 前端据此自动退出登录并跳转到登录页
 */
@Component
public class HeartbeatHandler extends TextWebSocketHandler {

    private static final Logger log = LoggerFactory.getLogger(HeartbeatHandler.class);
    private static final long HEARTBEAT_INTERVAL_MS = 5000; // 5 秒推送一次
    
    private final ConcurrentHashMap<String, WebSocketSession> sessions = new ConcurrentHashMap<>();
    private final ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor();
    private final ObjectMapper objectMapper;

    public HeartbeatHandler(ObjectMapper objectMapper) {
        this.objectMapper = objectMapper;
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
        
        String heartbeat = buildHeartbeatJson(sessions.size());
        
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
                String heartbeat = buildHeartbeatJson(null);
                session.sendMessage(new TextMessage(heartbeat));
            }
        } catch (IOException e) {
            log.warn("[WebSocket] 发送心跳失败", e);
        }
    }

    /**
     * 构建心跳 JSON 报文（使用 Jackson 序列化，避免手拼 JSON 字符串）。
     *
     * @param connections 当前连接数；为 null 时不携带该字段（单播场景）
     */
    private String buildHeartbeatJson(Integer connections) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("type", "heartbeat");
        payload.put("timestamp", Instant.now().toString());
        if (connections != null) {
            payload.put("connections", connections);
        }
        try {
            return objectMapper.writeValueAsString(payload);
        } catch (JsonProcessingException e) {
            // 理论不会发生（payload 全为基本类型）；降级返回最小报文，保证前端仍能收到心跳
            log.warn("[WebSocket] 心跳报文序列化失败，降级返回最小报文", e);
            return "{\"type\":\"heartbeat\"}";
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
