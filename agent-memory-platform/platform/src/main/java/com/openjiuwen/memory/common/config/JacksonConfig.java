package com.openjiuwen.memory.common.config;

import com.fasterxml.jackson.databind.PropertyNamingStrategies;
import org.springframework.boot.autoconfigure.jackson.Jackson2ObjectMapperBuilderCustomizer;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * Jackson 全局 SNAKE_CASE，对齐记忆服务 :8516 的字段命名
 * （mem_id / scope_id / page_idx / memory_type / user_id 等）。
 */
@Configuration
public class JacksonConfig {

        @Bean
        public Jackson2ObjectMapperBuilderCustomizer snakeCaseCustomizer() {
            return builder -> builder.propertyNamingStrategy(PropertyNamingStrategies.SNAKE_CASE);
        }
}
