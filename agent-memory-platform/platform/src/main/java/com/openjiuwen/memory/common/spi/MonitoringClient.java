package com.openjiuwen.memory.common.spi;

/**
 * 指标/巡检（属"监控巡检"模块）。本模块不实现，仅预留。
 */
public interface MonitoringClient {

    void gauge(String name, double value);

    void increment(String counter);
}
