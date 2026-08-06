package com.openjiuwen.memory.common.spi;

/**
 * 二次确认令牌服务（属"安全中心"模块）。
 * <p>
 * 针对高危操作（如内核重启、架构参数修改），在操作前要求二次确认：
 * <ol>
 *   <li>前端发起预检请求 → {@link #issue} 签发一次性令牌</li>
 *   <li>前端将令牌随实际操作请求提交 → 服务层 {@link #validate} 校验</li>
 *   <li>校验通过后 {@link #consume} 消费令牌（一次性，防重放）</li>
 * </ol>
 * <p>
 * 缺省实现（{@code SpiDefaults}）为 noop：issue 返回固定占位串，validate 恒 true。
 * 安全中心接入后以 @Bean 覆盖即可激活真实校验。
 */
public interface ConfirmTokenService {

    /**
     * 签发二次确认令牌。
     *
     * @param operator 操作人
     * @param action   操作类型（如 KERNEL_RESTART）
     * @param resource 操作目标（如 kernel）
     * @return 一次性令牌
     */
    String issue(String operator, String action, String resource);

    /**
     * 校验令牌有效性（未消费）。
     *
     * @param token    令牌
     * @param operator 操作人
     * @param action   操作类型
     * @param resource 操作目标
     * @return true=有效
     */
    boolean validate(String token, String operator, String action, String resource);

    /**
     * 消费令牌（使其失效，防重放攻击）。
     *
     * @param token 令牌
     */
    void consume(String token);
}
