FROM mcr.microsoft.com/dotnet/sdk:8.0

WORKDIR /src
COPY SlotAutoPlay/SlotAutoPlay.csproj SlotAutoPlay/
RUN dotnet restore SlotAutoPlay/SlotAutoPlay.csproj

COPY SlotAutoPlay/ SlotAutoPlay/
RUN dotnet publish SlotAutoPlay/SlotAutoPlay.csproj \
    --configuration Release \
    --output /app/publish \
    --no-restore

RUN pwsh SlotAutoPlay/bin/Release/net8.0/playwright.ps1 install --with-deps chromium

WORKDIR /app
COPY --from=0 /app/publish/ ./

ENV SLOT_AUTOPLAY_BASE_PATH=/data
VOLUME ["/data"]

ENTRYPOINT ["dotnet", "SlotAutoPlay.dll"]