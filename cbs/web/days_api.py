"""The single partner-scoped editor for dates and habits."""
from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from cbs.days import ImportantDay, Habit


class DaysInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=0)
    days: list[ImportantDay]
    habits: list[Habit]


def install_days(app, selected):
    @app.get('/api/partner/days')
    def get_days(p=Depends(selected)):
        return p.store.days_state(p.config.character.id)

    @app.put('/api/partner/days')
    def put_days(body: DaysInput, p=Depends(selected)):
        try:
            with p._lock:
                days = [{**d.model_dump(), 'name': p.client.sanitize_text(d.name)} for d in body.days]
                habits = [{**h.model_dump(), 'name': p.client.sanitize_text(h.name)} for h in body.habits]
                return p.store.save_days(p.config.character.id, days, habits, body.version)
        except ValueError:
            raise HTTPException(422, '日付・曜日・名前・版を確認してください。変更が競合した場合は読み直してください。') from None
