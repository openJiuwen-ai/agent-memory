package com.openjiuwen.memory;

import com.openjiuwen.memory.opscenter.service.OpsCommandService;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 上下文冒烟测试：用 test profile（SQLite 文件库），不启 Web 服务器、不调 :8516。
 * 验证 Spring 上下文 + MyBatis-Plus + 建表 + 命令目录种子能正常装配。
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.NONE)
@ActiveProfiles("test")
class MemoryServiceApplicationTest {

    @Autowired
    OpsCommandService opsCommandService;

    @Test
    void contextLoads() {
        assertThat(opsCommandService).isNotNull();
    }
}
