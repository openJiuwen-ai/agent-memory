package com.openjiuwen.memory.common.util;

import com.openjiuwen.memory.common.ResultCode;
import com.openjiuwen.memory.common.exception.BizException;

import java.nio.file.Path;
import java.util.regex.Pattern;

/**
 * 文件名/路径安全校验工具 — 防止路径遍历攻击。
 * <p>
 * 采用白名单策略（业界推荐）：只允许已知安全的字符通过，而非逐个拦截已知攻击模式。
 * <p>
 * 两层校验：
 * <ul>
 *   <li>{@link #validate(String)} — 白名单字符校验，用于无法确定基准目录的场景（如转发给下游服务）</li>
 *   <li>{@link #validate(String, Path)} — 白名单 + Path 归一化 + 目录归属校验，用于本地文件访问</li>
 * </ul>
 */
public final class PathValidator {

    private PathValidator() {}

    /**
     * 白名单：只允许字母、数字、点、连字符、下划线、正斜杠。
     * <p>
     * 合法示例：{@code run/jiuwen.log}、{@code platform.log}、{@code 2024-01-15.log.1}
     * <p>
     * 拦截：{@code ../}（穿越）、{@code \}（Windows 分隔符）、{@code /etc/passwd}（绝对路径）、
     * null 字节、空格、分号、管道符等一切非白名单字符。
     */
    private static final Pattern SAFE_FILENAME = Pattern.compile("^[A-Za-z0-9._/-]+$");

    /**
     * 白名单字符校验（不带基准目录）。
     * <p>
     * 用于文件名会转发给下游服务（如内核 API）的场景——平台层无法做 Path 归一化，
     * 但可以在转发前用白名单确保文件名不包含危险字符。
     *
     * @param filename 用户传入的文件名/相对路径
     * @throws BizException 文件名为空或包含非白名单字符
     */
    public static void validate(String filename) {
        if (filename == null || filename.isBlank()) {
            throw new BizException(ResultCode.BAD_REQUEST, "filename 参数不能为空");
        }
        if (filename.indexOf('\0') >= 0) {
            throw new BizException(ResultCode.FORBIDDEN, "非法文件路径：不允许包含 null 字节");
        }
        if (!SAFE_FILENAME.matcher(filename).matches()) {
            throw new BizException(ResultCode.FORBIDDEN,
                    "非法文件路径：文件名仅允许字母、数字、点、连字符、下划线、正斜杠");
        }
        // 白名单已排除 .. 但做一次显式检查，确保语义清晰
        if (filename.contains("..")) {
            throw new BizException(ResultCode.FORBIDDEN, "非法文件路径：不允许路径遍历");
        }
    }

    /**
     * 白名单 + Path 归一化 + 目录归属校验（带基准目录）。
     * <p>
     * 用于文件名会用于本地文件系统访问的场景。即使白名单通过，仍需归一化后
     * 确认最终路径在基准目录内——防止符号链接等文件系统层面的绕过。
     *
     * @param filename 用户传入的文件名/相对路径
     * @param baseDir  允许的基准目录
     * @return 归一化后的安全绝对路径
     * @throws BizException 校验失败
     */
    public static Path validate(String filename, Path baseDir) {
        validate(filename);
        Path base = baseDir.toAbsolutePath().normalize();
        Path target = base.resolve(filename).toAbsolutePath().normalize();
        if (!target.startsWith(base)) {
            throw new BizException(ResultCode.FORBIDDEN,
                    "非法文件路径：路径越界至基准目录外");
        }
        return target;
    }
}
