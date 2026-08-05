"""显式关闭限流的实现：恒放行（security.md §8.1）。

存在的理由是 TRUSTED 模式的真实部署形态——网关已在边缘做了限流，框架再做
一层只会把「网关的单个出口 IP」当成一个 peer，从而把全部正常流量误伤成 429。
这种部署需要一个**写在配置里、看得见**的关闭方式，而不是把 ``capacity`` 写成
某个反着读的魔法值（``capacity: 0`` 是「一个令牌都不给」还是「不限流」？
配置文件里读不出来，而读不出来的配置就是会被写错的配置）。
"""

from __future__ import annotations

from common.admission.base import RateLimiter, RateLimitProducer


class NoRateLimit(RateLimiter):
    """恒放行。"""

    def allow(self, peer: str) -> bool:
        return True

    def health(self) -> None:
        return None


@RateLimitProducer.register("unlimited")
def _build(config):
    return NoRateLimit()
