from .notifier import DiscordNotifier, NullNotifier
from .product_policy import install_discord_product_policy

install_discord_product_policy()

__all__ = ["DiscordNotifier", "NullNotifier"]
