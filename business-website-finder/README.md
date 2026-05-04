# Business Website Finder + Email Outreach Bot

Automatically finds local service businesses without websites on Google Maps, then sends personalized cold outreach emails offering affordable web design.

## Setup

```bash
cd business-website-finder
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
# Edit .env with your Gmail address and App Password
# Edit config.yaml with your target city and state
```

### Gmail App Password
1. Enable 2FA on your Google account
2. Go to Google Account → Security → App Passwords
3. Create a new app password and paste it into `.env`

## Usage

```bash
# Full run (find businesses + find emails + send emails)
python main.py

# Preview emails without sending
python main.py --dry-run

# Only scan Google Maps and find emails (skip sending)
python main.py --phase maps

# Only send emails to businesses already in data/contacted.json
python main.py --phase email
```

## How It Works

1. **Phase 1 — Maps Scan**: Playwright scrapes Google Maps for each business type in your config, detects which listings have no website link, and collects their name/address/phone/category.

2. **Phase 2 — Email Discovery**: For each no-website business, tries to find a contact email from:
   - Google Maps listing description
   - Yelp business page
   - Facebook Business page

3. **Phase 3 — Email Send**: Sends a personalized email via Gmail SMTP. Construction companies (general contractors, roofers, etc.) get a special line: *"My dad is a general contractor..."*

## Email Template Preview

> Hi there,
>
> I was looking for roofing services in Austin and came across Smith Roofing LLC. I noticed you didn’t have a website listed — I just wanted to reach out because I build websites specifically for service-based businesses like yours.
>
> *(construction only)* My dad is a general contractor, so I’ve grown up around the trades...
>
> I’m in college majoring in web design and development, and I take on website projects to help pay for school. That means I keep my prices very reasonable — usually $150–$300...
>
> I’d love to send over a free mock-up with no strings attached.
>
> Thanks for your time,
> Nick Volpe

## Data Files

| File | Contents |
|------|----------|
| `data/contacted.json` | Businesses that have been emailed |
| `data/no_email_found.json` | Businesses found but no email discovered |
| `logs/bot.log` | Full run log |

These files are gitignored. On re-runs the bot skips businesses it has already seen.
