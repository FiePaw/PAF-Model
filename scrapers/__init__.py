"""
scrapers package — PAF-Model browser-automation scrapers.

Two backends live side by side as parallel modules, each with its own proven
base class (account-name model for DeepSeek, cookie-file model for Qwen):

    base_deepseek.BaseAIChatScraper  ← DeepSeekScraper
    base_qwen.BaseAIChatScraper      ← QwenScraper
"""
from .deepseek_scraper import DeepSeekScraper
from .qwen_scraper import QwenScraper

__all__ = ["DeepSeekScraper", "QwenScraper"]
