from abc import ABC, abstractmethod


class Tool(ABC):
    name: str
    requires_approval: bool = False  # tool destruktif perlu approval

    @abstractmethod
    async def execute(self, input_data: dict, vault) -> dict: ...

    @abstractmethod
    def schema(self) -> dict: ...
