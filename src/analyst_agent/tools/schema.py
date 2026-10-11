"""describe_schema: built from the allowlist, so blocked columns are never named."""

from __future__ import annotations

from strands import tool

from analyst_agent.safety.policy import Access, DataPolicy


NOTES = {
    Access.PSEUDONYMIZE: " [person ID: join/count only, shown as a pseudonym]",
    Access.QUASI_IDENTIFIER: " [demographic: only for groups of customers, never per customer]",
}


def render_schema(policy: DataPolicy) -> str:
    """Schema text for prompts, listing only allowed columns with their access notes."""
    lines = [f"Dataset: {policy.dataset}", ""]
    for table_name, table in policy.tables.items():
        lines.append(f"Table `{table_name}`: {table.description}")
        for name, col in table.columns.items():
            note = NOTES.get(col.access, "")
            lines.append(f"  - {name} ({col.type}): {col.description}{note}")
        lines.append("")
    lines.append("Personal identifiers (names, emails, addresses, exact locations) are not available.")
    return "\n".join(lines)


def make_describe_schema(policy: DataPolicy):
    """Builds the describe_schema tool from the policy."""
    schema_text = render_schema(policy)

    @tool
    def describe_schema() -> str:
        """Describe the tables and columns available for analysis."""
        return schema_text

    return describe_schema
