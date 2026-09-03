from sqlalchemy import select, delete
from sqlalchemy.exc import IntegrityError
from infra.db.session import async_session
from models.user import User
from models.watchlist import Watchlist


async def get_or_create_user(telegram_id: int, username: str = None, first_name: str = None):
    """Registers a user in the DB if they don't exist."""
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        
        if not user:
            user = User(telegram_id=telegram_id, username=username, first_name=first_name)
            session.add(user)
            await session.commit()
        return user


async def add_to_watchlist(user_id: int, contract: str) -> bool:
    """Adds a token to the user's watchlist. Returns False if already exists."""
    async with async_session() as session:
        try:
            new_item = Watchlist(user_id=user_id, contract=contract)
            session.add(new_item)
            await session.commit()
            return True
        except IntegrityError:
            await session.rollback()
            return False


async def remove_from_watchlist(user_id: int, contract: str) -> bool:
    """Removes a token from the watchlist. Returns True if deleted."""
    async with async_session() as session:
        result = await session.execute(
            delete(Watchlist).where(
                Watchlist.user_id == user_id, 
                Watchlist.contract == contract
            )
        )
        await session.commit()
        return result.rowcount > 0


async def get_user_watchlist(user_id: int) -> list:
    """Fetches all tokens in a user's watchlist."""
    async with async_session() as session:
        result = await session.execute(
            select(Watchlist).where(Watchlist.user_id == user_id).order_by(Watchlist.added_at.desc())
        )
        return result.scalars().all()
