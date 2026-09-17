"""Business analyst bot: find, screen and analyse businesses acquirable with no cash down."""

__version__ = "0.1.0"

from .models import (
    AnalysisSection,
    DealReport,
    DealScore,
    DealStage,
    Listing,
    NoteTerms,
    OfferStructure,
    SellerFinancing,
)
from .pipeline import Pipeline, PipelineConfig, RunStats
from .store import DealStore

__all__ = [
    "__version__",
    "Listing",
    "DealReport",
    "DealScore",
    "DealStage",
    "NoteTerms",
    "OfferStructure",
    "SellerFinancing",
    "AnalysisSection",
    "Pipeline",
    "PipelineConfig",
    "RunStats",
    "DealStore",
]
