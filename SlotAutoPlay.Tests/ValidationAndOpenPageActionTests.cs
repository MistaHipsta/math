using SlotAutoPlay.Models;
using Xunit;

namespace SlotAutoPlay.Tests;

public sealed class ValidationAndOpenPageActionTests
{
    [Theory]
    [InlineData("--jobs")]
    [InlineData("--jobs", "not-a-number")]
    [InlineData("--jobs", "0")]
    [InlineData("--jobs", "-1")]
    public void InvalidJobsValueIsRejected(params string[] args)
    {
        var exception = Assert.Throws<ArgumentException>(
            () => SlotAutoPlay.Program.ParseJobsOption(args, defaultValue: 1));

        Assert.Contains("--jobs", exception.Message);
    }

    [Fact]
    public void MissingJobsOptionKeepsDefault()
    {
        Assert.Equal(
            1,
            SlotAutoPlay.Program.ParseJobsOption([], defaultValue: 1));
    }

    [Fact]
    public void WaitForPageLoadIsAnActionWithoutClick()
    {
        var action = OpenPageAction.ForCommand(
            BrowserAutomation.WAIT_FOR_PAGE_LOAD);

        Assert.True(action.WaitForPageLoad);
        Assert.Null(action.Click);
    }

    [Fact]
    public void OpenPageSequenceCanMixClickAndWaitActions()
    {
        var sequence = new[]
        {
            OpenPageAction.ForClick(new ScreenRect(10, 20, 10, 10)),
            OpenPageAction.ForCommand(BrowserAutomation.WAIT_FOR_PAGE_LOAD),
            OpenPageAction.ForClick(new ScreenRect(30, 40, 10, 10))
        };

        Assert.Equal(3, sequence.Length);
        Assert.NotNull(sequence[0].Click);
        Assert.True(sequence[1].WaitForPageLoad);
        Assert.Null(sequence[1].Click);
        Assert.NotNull(sequence[2].Click);
    }
}