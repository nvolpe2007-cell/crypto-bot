import os
from dataclasses import dataclass

SENDER_NAME = os.getenv("SENDER_NAME", "Nick Volpe")

CONSTRUCTION_CATEGORIES: frozenset[str] = frozenset({
    "general contractor",
    "roofing contractor",
    "roof inspector",
    "remodeling contractor",
    "home builder",
    "construction company",
    "demolition contractor",
    "concrete contractor",
    "masonry contractor",
    "drywall contractor",
    "insulation contractor",
    "siding contractor",
    "renovation contractor",
    "framing contractor",
    "foundation contractor",
    "excavating contractor",
    "swimming pool contractor",
})


def is_construction(category: str) -> bool:
    return category.strip().lower() in CONSTRUCTION_CATEGORIES


@dataclass
class TemplateContext:
    business_name: str
    is_construction: bool
    service_type: str
    city: str


_INTRO = (
    "Hi there,\n\n"
    "I was looking for {service_type} services in {city} and came across {business_name}. "
    "I noticed you didn't have a website listed — I just wanted to reach out because "
    "I build websites specifically for service-based businesses like yours."
)

_CONSTRUCTION = (
    "My dad is a general contractor, so I've grown up around the trades — "
    "I know how busy the day-to-day gets and how much a professional online presence "
    "can mean for getting more calls and standing out from the competition."
)

_COLLEGE = (
    "I'm in college majoring in web design and development, and I take on website "
    "projects to help pay for school. That means I keep my prices very reasonable — "
    "usually $150–$300 for a clean, professional site that shows up on Google and "
    "makes it easy for customers to contact you."
)

_CTA = (
    "A simple website can make a big difference — customers searching online are "
    "much more likely to call a business that has one. I'd love to send over a free "
    "mock-up with no strings attached. Just reply here or give me a call."
)

_SIGNATURE = "Thanks for your time,\n{sender_name}"


def build_subject(ctx: TemplateContext) -> str:
    return f"Quick question about {ctx.business_name}"


def build_body(ctx: TemplateContext) -> str:
    parts = [_INTRO.format(
        service_type=ctx.service_type,
        city=ctx.city,
        business_name=ctx.business_name,
    )]
    if ctx.is_construction:
        parts.append(_CONSTRUCTION)
    parts.append(_COLLEGE)
    parts.append(_CTA)
    parts.append(_SIGNATURE.format(sender_name=SENDER_NAME))
    return "\n\n".join(parts)


def render_template(ctx: TemplateContext) -> tuple[str, str]:
    return build_subject(ctx), build_body(ctx)
