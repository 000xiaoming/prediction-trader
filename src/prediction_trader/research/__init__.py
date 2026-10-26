"""Optional offline research integration, isolated from the trading decision path."""

from prediction_trader.research.context import (
    FileResearchContextProvider,
    ResearchContext,
    write_research_context,
)

__all__ = ["FileResearchContextProvider", "ResearchContext", "write_research_context"]
