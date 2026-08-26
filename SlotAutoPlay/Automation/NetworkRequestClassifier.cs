namespace SlotAutoPlay.Automation;

public static class NetworkRequestClassifier
{
    private static readonly string[] AssetExtensions =
    [
        ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg",
        ".woff", ".woff2", ".map", ".ico", ".webp", ".avif"
    ];

    public static bool IsAssetUrl(string url)
    {
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri))
        {
            return true;
        }

        return AssetExtensions.Any(extension =>
            uri.AbsolutePath.EndsWith(extension, StringComparison.OrdinalIgnoreCase));
    }

    public static bool IsCandidate(
        string url,
        string method,
        string? contentType = null)
    {
        return !IsAssetUrl(url) &&
               method is "POST" or "PUT" or "PATCH";
    }

    public static bool IsAuthoritative(
        string url,
        string method,
        string? expectedHost,
        string? expectedPath)
    {
        return IsAuthoritative(
            url,
            method,
            contentType: "application/json",
            expectedHost,
            expectedPath,
            expectedMethod: method,
            expectedContentType: "application/json");
    }

    public static bool IsAuthoritative(
        string url,
        string method,
        string? contentType,
        string? expectedHost,
        string? expectedPath,
        string? expectedMethod,
        string? expectedContentType)
    {
        if (!IsCandidate(url, method, contentType) ||
            string.IsNullOrWhiteSpace(expectedHost) ||
            string.IsNullOrWhiteSpace(expectedPath) ||
            string.IsNullOrWhiteSpace(expectedMethod) ||
            string.IsNullOrWhiteSpace(expectedContentType) ||
            !Uri.TryCreate(url, UriKind.Absolute, out var uri))
        {
            return false;
        }

        return string.Equals(
                   uri.Host,
                   expectedHost,
                   StringComparison.OrdinalIgnoreCase) &&
               PathMatches(uri.AbsolutePath, expectedPath) &&
               string.Equals(
                   method,
                   expectedMethod,
                   StringComparison.OrdinalIgnoreCase) &&
               ContentTypeMatches(contentType, expectedContentType);
    }

    private static bool PathMatches(string actual, string expected)
    {
        if (string.Equals(actual, expected, StringComparison.Ordinal))
        {
            return true;
        }

        const string placeholder = "{session}";
        var expectedParts = expected.Split('/');
        var actualParts = actual.Split('/');
        if (expectedParts.Length != actualParts.Length)
        {
            return false;
        }

        for (var index = 0; index < expectedParts.Length; index++)
        {
            if (string.Equals(
                    expectedParts[index],
                    placeholder,
                    StringComparison.Ordinal))
            {
                if (string.IsNullOrEmpty(actualParts[index]))
                {
                    return false;
                }

                continue;
            }

            if (!string.Equals(
                    actualParts[index],
                    expectedParts[index],
                    StringComparison.Ordinal))
            {
                return false;
            }
        }

        return true;
    }

    public static bool ContentTypeMatches(
        string? actual,
        string expected)
    {
        if (string.IsNullOrWhiteSpace(actual))
        {
            return false;
        }

        var actualMediaType = actual.Split(';', 2)[0].Trim();
        var expectedMediaType = expected.Split(';', 2)[0].Trim();
        return string.Equals(
            actualMediaType,
            expectedMediaType,
            StringComparison.OrdinalIgnoreCase);
    }
}