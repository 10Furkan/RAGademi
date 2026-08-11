"""Runtime settings loaded from environment variables or a .env file."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="DERSNOTU_", extra="ignore"
    )

    # --- Models ---------------------------------------------------------
    # Main generation model and inexpensive classification/topic-card model.
    model: str = "claude-sonnet-5"
    cheap_model: str = "claude-haiku-4-5"
    # Empty means: use the model selected by Codex CLI.
    codex_model: str = ""
    # low | medium | high | xhigh | max
    effort: str = "high"
    max_tokens: int = 16000

    # --- Directories ----------------------------------------------------
    cache_dir: Path = Path(".cache")
    out_dir: Path = Path("out")

    # --- Retrieval ------------------------------------------------------
    chunk_target_tokens: int = 900
    chunk_overlap_ratio: float = 0.15
    chunks_per_section: int = 6
    # Extract textbook diagrams for insertion into generated documents.
    extract_book_figures: bool = True
    # Maximum textbook figures offered to the model per section.
    figures_per_section: int = 6

    # --- Lecture PDF ----------------------------------------------------
    # Vector-object threshold for treating a slide as visual.
    visual_shape_threshold: int = 20
    # Maximum slides in one expansion call.
    max_section_slides: int = 8
    # Maximum slide images sent to a model.
    max_slide_images: int = 30
    # Long edge of a rendered slide. Image-token cost scales with area.
    slide_image_max_edge: int = 1400

    # --- Library --------------------------------------------------------
    # Persistent courses, uploaded materials, and generated documents.
    library_name: str = "library.sqlite"

    @property
    def library_path(self) -> Path:
        return self.cache_dir / self.library_name

    @property
    def materials_dir(self) -> Path:
        return self.cache_dir / "materials"

    def ensure_dirs(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
