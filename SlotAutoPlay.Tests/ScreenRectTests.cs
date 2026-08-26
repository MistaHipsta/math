using SlotAutoPlay.Models;
using Xunit;

namespace SlotAutoPlay.Tests;

public sealed class ScreenRectTests
{
    [Fact]
    public void CenterPointUsesAbsoluteCoordinates()
    {
        var rect = new ScreenRect(100, 200, 41, 21);

        Assert.Equal((120, 210), rect.GetClickPoint());
        Assert.Equal(120, rect.CenterX);
        Assert.Equal(210, rect.CenterY);
    }

    [Theory]
    [InlineData(0, 10)]
    [InlineData(10, 0)]
    [InlineData(-1, 10)]
    [InlineData(10, -1)]
    public void NonPositiveSizeIsRejected(int width, int height)
    {
        Assert.Throws<ArgumentOutOfRangeException>(
            () => new ScreenRect(0, 0, width, height));
    }

    [Fact]
    public void RandomPointStaysInsideRectangle()
    {
        var rect = new ScreenRect(10, 20, 30, 40);
        var random = new Random(123);

        for (var index = 0; index < 100; index++)
        {
            var point = rect.GetClickPoint(random, randomize: true);

            Assert.InRange(point.X, rect.X, rect.X + rect.Width - 1);
            Assert.InRange(point.Y, rect.Y, rect.Y + rect.Height - 1);
        }
    }
}