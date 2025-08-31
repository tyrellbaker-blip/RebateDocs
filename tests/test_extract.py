"""
Comprehensive unit tests for the extraction process.

These tests verify the functionality of the rebate document extraction system
following test-driven development principles.
"""

import pytest
from typing import List, Dict, Any, Tuple
from app.models.schemas import Span, KV, DocResult
from app.services.extract import (
    extract,
    normalize_amount,
    iso_date_or_none,
    lines_from_spans,
    build_toc_index,
    normalize_rebate_name,
    choose_toc_for_page,
    split_models,
    parse_exclusions_from_text,
    is_label_text,
    detect_model_year_model_trim,
    parse_trim_and_amounts_from_line,
)


class TestNormalizeAmount:
    """Test the normalize_amount function."""
    
    def test_valid_amounts(self):
        """Test normalization of valid dollar amounts."""
        assert normalize_amount("$1,500") == 1500
        assert normalize_amount("$500") == 500
        assert normalize_amount("$10,000") == 10000
        assert normalize_amount("1500") == 1500
        assert normalize_amount("$1,234,567") == 1234567
    
    def test_invalid_amounts(self):
        """Test handling of invalid amounts."""
        assert normalize_amount("invalid") is None
        assert normalize_amount("$1.5k") is None
        assert normalize_amount("") is None
        assert normalize_amount("$") is None
        assert normalize_amount("$abc") is None


class TestIsoDateOrNone:
    """Test the iso_date_or_none function."""
    
    def test_valid_dates(self):
        """Test conversion of valid dates to ISO format."""
        assert iso_date_or_none("8/1/2025") == "2025-08-01"
        assert iso_date_or_none("12/31/2024") == "2024-12-31"
        assert iso_date_or_none("1/1/2025") == "2025-01-01"
        assert iso_date_or_none("8-1-2025") == "2025-08-01"
    
    def test_invalid_dates(self):
        """Test handling of invalid dates."""
        assert iso_date_or_none("invalid") is None
        assert iso_date_or_none("13/32/2024") is None
        assert iso_date_or_none("") is None
        assert iso_date_or_none("8/1") is None


class TestLinesFromSpans:
    """Test the lines_from_spans function."""
    
    def test_group_spans_by_line(self):
        """Test grouping spans by page and line_id."""
        spans = [
            Span(text="Hello", bbox=(0, 0, 50, 10), page=1, line_id=1),
            Span(text="World", bbox=(60, 0, 110, 10), page=1, line_id=1),
            Span(text="Second", bbox=(0, 15, 50, 25), page=1, line_id=2),
            Span(text="Line", bbox=(60, 15, 90, 25), page=1, line_id=2),
        ]
        
        result = lines_from_spans(spans)
        
        assert result[(1, 1)] == "Hello World"
        assert result[(1, 2)] == "Second Line"
    
    def test_spans_sorted_left_to_right(self):
        """Test that spans are sorted left to right within lines."""
        spans = [
            Span(text="World", bbox=(60, 0, 110, 10), page=1, line_id=1),  # Right
            Span(text="Hello", bbox=(0, 0, 50, 10), page=1, line_id=1),    # Left
        ]
        
        result = lines_from_spans(spans)
        
        assert result[(1, 1)] == "Hello World"


class TestBuildTocIndex:
    """Test the build_toc_index function."""
    
    def test_parse_toc_entries(self):
        """Test parsing of TOC entries."""
        lines = {
            (1, 1): "Program ID Program Name Updated Page(s)",
            (1, 2): "V25URC08 Retail Customer Bonus 8/1/2025 10-11",
            (1, 3): "V25UAE08 Dealer Bonus - EV 8/15/2025 12-15",
        }
        
        toc = build_toc_index(lines)
        
        assert len(toc) == 2
        
        entry1 = toc[0]
        assert entry1["program_id"] == "V25URC08"
        assert entry1["program_name"] == "Retail Customer Bonus"
        assert entry1["updated_iso"] == "2025-08-01"
        assert entry1["pages"] == [10, 11]
        
        entry2 = toc[1]
        assert entry2["program_id"] == "V25UAE08"
        assert entry2["program_name"] == "Dealer Bonus - EV"
        assert entry2["updated_iso"] == "2025-08-15"
        assert entry2["pages"] == [12, 13, 14, 15]
    
    def test_parse_single_page_toc_entry(self):
        """Test parsing TOC entry with single page."""
        lines = {
            (1, 1): "Program ID Program Name Updated Page(s)",
            (1, 2): "V25URC08 Retail Customer Bonus 8/1/2025 10",
        }
        
        toc = build_toc_index(lines)
        
        assert len(toc) == 1
        assert toc[0]["pages"] == [10]
    
    def test_empty_toc(self):
        """Test handling of empty or invalid TOC."""
        lines = {(1, 1): "Some random text"}
        
        toc = build_toc_index(lines)
        
        assert len(toc) == 0


class TestNormalizeRebateName:
    """Test the normalize_rebate_name function."""
    
    def test_normalize_known_names(self):
        """Test normalization of known rebate names."""
        assert normalize_rebate_name("dealer bonus") == "Dealer Bonus"
        assert normalize_rebate_name("Dealer Bonus - EV") == "Dealer Bonus - EV"
        assert normalize_rebate_name("retail customer bonus") == "Retail Customer Bonus"
        assert normalize_rebate_name("apr customer bonus – ev") == "APR Customer Bonus - EV"
    
    def test_normalize_unknown_names(self):
        """Test handling of unknown rebate names."""
        assert normalize_rebate_name("Unknown Bonus Type") == "Unknown Bonus Type"
        assert normalize_rebate_name(None) is None


class TestChooseTocForPage:
    """Test the choose_toc_for_page function."""
    
    def test_choose_matching_toc_entry(self):
        """Test choosing TOC entry that covers the given page."""
        toc = [
            {"program_id": "V25URC08", "program_name": "Retail Customer Bonus", "pages": [10, 11]},
            {"program_id": "V25UAE08", "program_name": "Dealer Bonus", "pages": [12, 13]},
        ]
        
        result = choose_toc_for_page(toc, 11, None)
        
        assert result is not None
        assert result["program_id"] == "V25URC08"
    
    def test_choose_with_rebate_hint(self):
        """Test choosing TOC entry with rebate type hint."""
        toc = [
            {"program_id": "V25URC08", "program_name": "Retail Customer Bonus", "pages": [10, 11]},
            {"program_id": "V25UAE08", "program_name": "Dealer Bonus", "pages": [10, 11]},
        ]
        
        result = choose_toc_for_page(toc, 10, "dealer bonus")
        
        assert result is not None
        assert result["program_id"] == "V25UAE08"
    
    def test_no_matching_page(self):
        """Test handling when no TOC entry covers the page."""
        toc = [
            {"program_id": "V25URC08", "program_name": "Retail Customer Bonus", "pages": [10, 11]},
        ]
        
        result = choose_toc_for_page(toc, 15, None)
        
        assert result is None


class TestSplitModels:
    """Test the split_models function."""
    
    def test_split_ampersand_models(self):
        """Test splitting models connected by ampersand."""
        assert split_models("Atlas & Atlas Cross Sport") == ["Atlas", "Atlas Cross Sport"]
    
    def test_split_slash_models(self):
        """Test splitting models connected by slash."""
        assert split_models("ID.4 / ID. Buzz") == ["ID.4", "ID. Buzz"]
    
    def test_split_comma_models(self):
        """Test splitting models connected by comma."""
        assert split_models("Tiguan, Taos, Atlas") == ["Tiguan", "Taos", "Atlas"]
    
    def test_single_model(self):
        """Test handling of single model."""
        assert split_models("Tiguan") == ["Tiguan"]
    
    def test_empty_model(self):
        """Test handling of empty model."""
        assert split_models("") == [""]


class TestParseExclusionsFromText:
    """Test the parse_exclusions_from_text function."""
    
    def test_parse_parenthetical_exclusions(self):
        """Test parsing exclusions in parentheses."""
        text = "Tiguan $1,500 (excludes base trim)"
        result = parse_exclusions_from_text(text)
        assert result == "(excludes base trim)"
    
    def test_parse_trailing_exclusions(self):
        """Test parsing trailing exclusions."""
        text = "Atlas $2,000 excludes SE trim"
        result = parse_exclusions_from_text(text)
        assert result == "excludes SE trim"
    
    def test_no_exclusions(self):
        """Test handling text without exclusions."""
        text = "Tiguan $1,500"
        result = parse_exclusions_from_text(text)
        assert result is None


class TestIsLabelText:
    """Test the is_label_text function."""
    
    def test_recognize_known_labels(self):
        """Test recognition of known label text."""
        assert is_label_text("Retail Customer Bonus") == "retail customer bonus"
        assert is_label_text("Dealer Bonus") == "dealer bonus"
        assert is_label_text("APR Customer Bonus") == "apr customer bonus"
    
    def test_recognize_label_synonyms(self):
        """Test recognition of label synonyms."""
        assert is_label_text("Customer Bonus") == "retail customer bonus"
        assert is_label_text("Loyalty Code Bonus") == "loyalty bonus"
    
    def test_unrecognized_text(self):
        """Test handling of unrecognized text."""
        assert is_label_text("Random Text") is None


class TestDetectModelYearModelTrim:
    """Test the detect_model_year_model_trim function."""
    
    def test_detect_full_model_info(self):
        """Test detection of complete model year, model, and trim info."""
        text = "MY24 Tiguan SE $1,500"
        year, model, trim = detect_model_year_model_trim(text)
        
        assert year == 2024
        assert model == "Tiguan"
        assert trim == "SE"
    
    def test_detect_model_without_trim(self):
        """Test detection of model without trim."""
        text = "MY25 Atlas $2,000"
        year, model, trim = detect_model_year_model_trim(text)
        
        assert year == 2025
        assert model == "Atlas"
        assert trim is None
    
    def test_detect_full_year(self):
        """Test detection with full 4-digit year."""
        text = "2024 Tiguan SE $1,500"
        year, model, trim = detect_model_year_model_trim(text)
        
        assert year == 2024
        assert model == "Tiguan"
        assert trim == "SE"
    
    def test_no_bonus_as_model(self):
        """Test that 'Bonus' is never returned as a model."""
        text = "MY25 Bonus $1,500"
        year, model, trim = detect_model_year_model_trim(text)
        
        assert year == 2025
        assert model is None
        assert trim is None


class TestParseTrimAndAmountsFromLine:
    """Test the parse_trim_and_amounts_from_line function."""
    
    def test_parse_trim_and_single_amount(self):
        """Test parsing trim and single amount."""
        text = "SE $1,500"
        trim, amounts = parse_trim_and_amounts_from_line(text)
        
        assert trim == "SE"
        assert amounts == [1500]
    
    def test_parse_trim_and_multiple_amounts(self):
        """Test parsing trim and multiple amounts."""
        text = "SEL $2,000 $2,000"
        trim, amounts = parse_trim_and_amounts_from_line(text)
        
        assert trim == "SEL"
        assert amounts == [2000, 2000]
    
    def test_parse_no_trim(self):
        """Test parsing amounts without trim."""
        text = "$1,500"
        trim, amounts = parse_trim_and_amounts_from_line(text)
        
        assert trim is None
        assert amounts == [1500]
    
    def test_no_dollar_amounts(self):
        """Test handling text without dollar amounts."""
        text = "Just some text"
        trim, amounts = parse_trim_and_amounts_from_line(text)
        
        assert trim is None
        assert amounts == []
    
    def test_ignore_bonus_noise(self):
        """Test that 'Bonus' lines are ignored as trim."""
        text = "Bonus $1,500"
        trim, amounts = parse_trim_and_amounts_from_line(text)
        
        assert trim is None
        assert amounts == [1500]


class TestExtractIntegration:
    """Integration tests for the main extract function."""
    
    def test_extract_basic_rebate_data(self):
        """Test extraction of basic rebate data."""
        spans = [
            # TOC
            Span(text="Program", bbox=(0, 0, 50, 10), page=1, line_id=1),
            Span(text="ID", bbox=(60, 0, 80, 10), page=1, line_id=1),
            Span(text="Program", bbox=(90, 0, 140, 10), page=1, line_id=1),
            Span(text="Name", bbox=(150, 0, 180, 10), page=1, line_id=1),
            Span(text="Updated", bbox=(190, 0, 240, 10), page=1, line_id=1),
            Span(text="Page(s)", bbox=(250, 0, 290, 10), page=1, line_id=1),
            
            Span(text="V25URC08", bbox=(0, 15, 80, 25), page=1, line_id=2),
            Span(text="Retail", bbox=(90, 15, 130, 25), page=1, line_id=2),
            Span(text="Customer", bbox=(140, 15, 190, 25), page=1, line_id=2),
            Span(text="Bonus", bbox=(200, 15, 240, 25), page=1, line_id=2),
            Span(text="8/1/2025", bbox=(250, 15, 310, 25), page=1, line_id=2),
            Span(text="10", bbox=(320, 15, 340, 25), page=1, line_id=2),
            
            # Page 10 content
            Span(text="Retail", bbox=(0, 0, 50, 10), page=10, line_id=1),
            Span(text="Customer", bbox=(60, 0, 110, 10), page=10, line_id=1),
            Span(text="Bonus", bbox=(120, 0, 160, 10), page=10, line_id=1),
            
            Span(text="MY25", bbox=(0, 20, 40, 30), page=10, line_id=2),
            Span(text="Tiguan", bbox=(50, 20, 90, 30), page=10, line_id=2),
            
            Span(text="SE", bbox=(0, 35, 20, 45), page=10, line_id=3),
            Span(text="$1,500", bbox=(30, 35, 80, 45), page=10, line_id=3),
        ]
        
        result = extract("test_doc", spans)
        
        assert result.doc_id == "test_doc"
        assert len(result.kvs) == 1
        
        kv = result.kvs[0]
        assert kv.rebate_type == "Retail Customer Bonus"
        assert kv.program_id == "V25URC08"
        assert kv.published_date == "2025-08-01"
        assert kv.model_year == 2025
        assert kv.model == "Tiguan"
        assert kv.trim == "SE"
        assert kv.amount_dollars == 1500
        assert kv.page == 10
    
    def test_extract_multiple_models(self):
        """Test extraction with multiple models."""
        spans = [
            # Header
            Span(text="MY25", bbox=(0, 0, 40, 10), page=1, line_id=1),
            Span(text="Atlas", bbox=(50, 0, 90, 10), page=1, line_id=1),
            
            # Multiple trims
            Span(text="SE", bbox=(0, 15, 20, 25), page=1, line_id=2),
            Span(text="$2,000", bbox=(30, 15, 80, 25), page=1, line_id=2),
            
            Span(text="SEL", bbox=(0, 30, 30, 40), page=1, line_id=3),
            Span(text="$2,500", bbox=(40, 30, 90, 40), page=1, line_id=3),
        ]
        
        result = extract("test_doc", spans)
        
        assert len(result.kvs) == 2
        
        # Check first KV
        kv1 = result.kvs[0]
        assert kv1.model_year == 2025
        assert kv1.model == "Atlas"
        assert kv1.trim == "SE"
        assert kv1.amount_dollars == 2000
        
        # Check second KV
        kv2 = result.kvs[1]
        assert kv2.model_year == 2025
        assert kv2.model == "Atlas"
        assert kv2.trim == "SEL"
        assert kv2.amount_dollars == 2500
    
    def test_extract_with_exclusions(self):
        """Test extraction with exclusions."""
        spans = [
            Span(text="Tiguan", bbox=(0, 0, 50, 10), page=1, line_id=1),
            Span(text="$1,500", bbox=(60, 0, 110, 10), page=1, line_id=1),
            Span(text="(excludes", bbox=(120, 0, 180, 10), page=1, line_id=1),
            Span(text="base", bbox=(190, 0, 220, 10), page=1, line_id=1),
            Span(text="trim)", bbox=(230, 0, 270, 10), page=1, line_id=1),
        ]
        
        result = extract("test_doc", spans)
        
        assert len(result.kvs) == 1
        kv = result.kvs[0]
        assert kv.exclusions == "(excludes base trim)"
    
    def test_extract_all_vehicles_context(self):
        """Test extraction with 'all vehicles' context."""
        spans = [
            Span(text="New,", bbox=(0, 0, 30, 10), page=1, line_id=1),
            Span(text="unused", bbox=(35, 0, 80, 10), page=1, line_id=1),
            Span(text="Volkswagen", bbox=(85, 0, 150, 10), page=1, line_id=1),
            Span(text="models", bbox=(155, 0, 200, 10), page=1, line_id=1),
            Span(text="$500", bbox=(210, 0, 250, 10), page=1, line_id=1),
        ]
        
        result = extract("test_doc", spans)
        
        assert len(result.kvs) == 1
        kv = result.kvs[0]
        assert kv.model == "all"
        assert kv.amount_dollars == 500
    
    def test_extract_money_range(self):
        """Test extraction of money ranges."""
        spans = [
            Span(text="Tiguan", bbox=(0, 0, 50, 10), page=1, line_id=1),
            Span(text="$1,000", bbox=(60, 0, 110, 10), page=1, line_id=1),
            Span(text="-", bbox=(115, 0, 125, 10), page=1, line_id=1),
            Span(text="$2,000", bbox=(130, 0, 180, 10), page=1, line_id=1),
        ]
        
        result = extract("test_doc", spans)
        
        assert len(result.kvs) == 2  # Both range endpoints
        amounts = sorted([kv.amount_dollars for kv in result.kvs])
        assert amounts == [1000, 2000]
    
    def test_extract_filters_programs_without_amounts(self):
        """Test that programs without amounts are filtered out."""
        spans = [
            # TOC entry with program
            Span(text="Program", bbox=(0, 0, 50, 10), page=1, line_id=1),
            Span(text="ID", bbox=(60, 0, 80, 10), page=1, line_id=1),
            Span(text="Program", bbox=(90, 0, 140, 10), page=1, line_id=1),
            Span(text="Name", bbox=(150, 0, 180, 10), page=1, line_id=1),
            Span(text="Updated", bbox=(190, 0, 240, 10), page=1, line_id=1),
            Span(text="Page(s)", bbox=(250, 0, 290, 10), page=1, line_id=1),
            
            Span(text="V25URC08", bbox=(0, 15, 80, 25), page=1, line_id=2),
            Span(text="Test", bbox=(90, 15, 130, 25), page=1, line_id=2),
            Span(text="Program", bbox=(140, 15, 190, 25), page=1, line_id=2),
            Span(text="8/1/2025", bbox=(200, 15, 260, 25), page=1, line_id=2),
            Span(text="10", bbox=(270, 15, 290, 25), page=1, line_id=2),
            
            # Page content without dollar amounts
            Span(text="Test", bbox=(0, 0, 50, 10), page=10, line_id=1),
            Span(text="Program", bbox=(60, 0, 110, 10), page=10, line_id=1),
            Span(text="Some", bbox=(0, 15, 50, 25), page=10, line_id=2),
            Span(text="text", bbox=(60, 15, 100, 25), page=10, line_id=2),
        ]
        
        result = extract("test_doc", spans)
        
        # Should have no KVs since the program has no amounts
        assert len(result.kvs) == 0


class TestExtractDocumentStructure:
    """Test extraction behavior with complex document structures."""
    
    def test_extract_program_header_values(self):
        """Test extraction of program header values."""
        spans = [
            # Program header fields with values on next lines
            Span(text="Program", bbox=(0, 0, 50, 10), page=1, line_id=1),
            Span(text="ID", bbox=(60, 0, 80, 10), page=1, line_id=1),
            
            Span(text="V25URC08", bbox=(0, 15, 80, 25), page=1, line_id=2),
            
            Span(text="Published", bbox=(0, 30, 70, 40), page=1, line_id=3),
            
            Span(text="8/1/2025", bbox=(0, 45, 80, 55), page=1, line_id=4),
            
            Span(text="Program", bbox=(0, 60, 50, 70), page=1, line_id=5),
            Span(text="Start", bbox=(60, 60, 90, 70), page=1, line_id=5),
            
            Span(text="8/1/2025", bbox=(0, 75, 80, 85), page=1, line_id=6),
            
            Span(text="Program", bbox=(0, 90, 50, 100), page=1, line_id=7),
            Span(text="End", bbox=(60, 90, 90, 100), page=1, line_id=7),
            
            Span(text="12/31/2025", bbox=(0, 105, 90, 115), page=1, line_id=8),
            
            # Some rebate data
            Span(text="Tiguan", bbox=(0, 120, 50, 130), page=1, line_id=9),
            Span(text="$1,500", bbox=(60, 120, 110, 130), page=1, line_id=9),
        ]
        
        result = extract("test_doc", spans)
        
        assert len(result.kvs) == 1
        kv = result.kvs[0]
        assert kv.program_id == "V25URC08"
        assert kv.published_date == "2025-08-01"
        assert kv.program_start_date == "2025-08-01"
        assert kv.program_end_date == "2025-12-31"
    
    def test_extract_inline_header_format(self):
        """Test extraction of inline header format."""
        spans = [
            # Inline header row
            Span(text="Program", bbox=(0, 0, 50, 10), page=1, line_id=1),
            Span(text="ID", bbox=(60, 0, 80, 10), page=1, line_id=1),
            Span(text="Published", bbox=(90, 0, 140, 10), page=1, line_id=1),
            Span(text="Program", bbox=(150, 0, 200, 10), page=1, line_id=1),
            Span(text="Start", bbox=(210, 0, 240, 10), page=1, line_id=1),
            Span(text="Program", bbox=(250, 0, 300, 10), page=1, line_id=1),
            Span(text="End", bbox=(310, 0, 340, 10), page=1, line_id=1),
            
            # Values row
            Span(text="V25URC08", bbox=(0, 15, 80, 25), page=1, line_id=2),
            Span(text="8/1/2025", bbox=(90, 15, 150, 25), page=1, line_id=2),
            Span(text="8/1/2025", bbox=(160, 15, 220, 25), page=1, line_id=2),
            Span(text="12/31/2025", bbox=(230, 15, 300, 25), page=1, line_id=2),
            
            # Rebate data
            Span(text="Tiguan", bbox=(0, 30, 50, 40), page=1, line_id=3),
            Span(text="$1,500", bbox=(60, 30, 110, 40), page=1, line_id=3),
        ]
        
        result = extract("test_doc", spans)
        
        assert len(result.kvs) == 1
        kv = result.kvs[0]
        assert kv.program_id == "V25URC08"
        assert kv.published_date == "2025-08-01"
        assert kv.program_start_date == "2025-08-01"
        assert kv.program_end_date == "2025-12-31"


class TestExtractKvGroups:
    """Test KV grouping functionality."""
    
    def test_kv_groups_created_correctly(self):
        """Test that KV groups are created correctly in provenance."""
        spans = [
            # Two different programs with TOC
            Span(text="Program", bbox=(0, 0, 50, 10), page=1, line_id=1),
            Span(text="ID", bbox=(60, 0, 80, 10), page=1, line_id=1),
            Span(text="Program", bbox=(90, 0, 140, 10), page=1, line_id=1),
            Span(text="Name", bbox=(150, 0, 180, 10), page=1, line_id=1),
            Span(text="Updated", bbox=(190, 0, 240, 10), page=1, line_id=1),
            Span(text="Page(s)", bbox=(250, 0, 290, 10), page=1, line_id=1),
            
            Span(text="V25URC08", bbox=(0, 15, 80, 25), page=1, line_id=2),
            Span(text="Retail", bbox=(90, 15, 130, 25), page=1, line_id=2),
            Span(text="Customer", bbox=(140, 15, 190, 25), page=1, line_id=2),
            Span(text="Bonus", bbox=(200, 15, 240, 25), page=1, line_id=2),
            Span(text="8/1/2025", bbox=(250, 15, 310, 25), page=1, line_id=2),
            Span(text="10", bbox=(320, 15, 340, 25), page=1, line_id=2),
            
            Span(text="V25UAE08", bbox=(0, 30, 80, 40), page=1, line_id=3),
            Span(text="Dealer", bbox=(90, 30, 130, 40), page=1, line_id=3),
            Span(text="Bonus", bbox=(140, 30, 180, 40), page=1, line_id=3),
            Span(text="8/15/2025", bbox=(190, 30, 250, 40), page=1, line_id=3),
            Span(text="11", bbox=(260, 30, 280, 40), page=1, line_id=3),
            
            # Page 10 - Program V25URC08
            Span(text="Tiguan", bbox=(0, 0, 50, 10), page=10, line_id=1),
            Span(text="$1,500", bbox=(60, 0, 110, 10), page=10, line_id=1),
            
            Span(text="Atlas", bbox=(0, 15, 40, 25), page=10, line_id=2),
            Span(text="$2,000", bbox=(50, 15, 100, 25), page=10, line_id=2),
            
            # Page 11 - Program V25UAE08
            Span(text="ID.4", bbox=(0, 0, 40, 10), page=11, line_id=1),
            Span(text="$3,000", bbox=(50, 0, 100, 10), page=11, line_id=1),
        ]
        
        result = extract("test_doc", spans)
        
        assert len(result.kvs) == 3
        
        # Check that groups are created
        assert "kv_groups" in result.provenance
        assert "kv_group_order" in result.provenance
        
        groups = result.provenance["kv_groups"]
        assert "V25URC08" in groups
        assert "V25UAE08" in groups
        
        # V25URC08 should have 2 KVs (Tiguan and Atlas)
        assert len(groups["V25URC08"]) == 2
        
        # V25UAE08 should have 1 KV (ID.4)
        assert len(groups["V25UAE08"]) == 1
        
        # Check group order
        group_order = result.provenance["kv_group_order"]
        assert group_order == ["V25URC08", "V25UAE08"]