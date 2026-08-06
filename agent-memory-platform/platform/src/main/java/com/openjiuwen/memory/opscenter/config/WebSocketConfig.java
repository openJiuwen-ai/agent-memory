package com.openjiuwen.memory.opscenter.config;

import org.springframework.context.annotation.Configuration;
import org.springframework.web.socket.config.annotation.EnableWebSocket;
import org.springframework.web.socket.config.annotation.WebSocketConfigurer;
import org.springframework.web.socket.config.annotation.WebSocketHandlerRegistry;
import com.openjiuwen.memory.opscenter.websocket.HeartbeatHandler;

/**
 * WebSocket 配置 - 提供前端心跳监控通道。
 */
@Configuration
@EnableWebSocket
public class WebSocketConfig implements WebSocketConfigurer {

    private final HeartbeatHandler heartbeatHandler;

    public WebSocketConfig(HeartbeatHandler heartbeatHandler) {
        this.heartbeatHandler = heartbeatHandler;
    }

    @Override
    public void registerWebSocketHandlers(WebSocketHandlerRegistry registry) {
        // 注册心跳端点，允许跨域（前端开发环境 localhost:5173）
        registry.addHandler(heartbeatHandler, "/ws/heartbeat")
                .setAllowedOrigins("*");
    }
}
