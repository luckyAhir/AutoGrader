/* Tries every subarray: correct, but O(n^2). */
#include <stdio.h>

static long long values[100000];

int main(void)
{
    int n;
    if (scanf("%d", &n) != 1) return 1;
    for (int i = 0; i < n; i++) scanf("%lld", &values[i]);
    long long best = values[0];
    for (int start = 0; start < n; start++) {
        long long sum = 0;
        for (int end = start; end < n; end++) {
            sum += values[end];
            if (sum > best) best = sum;
        }
    }
    printf("%lld\n", best);
    return 0;
}
