#include <stdio.h>

int main(void)
{
    int n;
    scanf("%d", &n);
    long long best = 0, cur = 0;   /* bug: an all-negative array prints 0 */
    for (int i = 0; i < n; i++) {
        long long x;
        scanf("%lld", &x);
        cur += x;
        if (cur > best) best = cur;
        if (cur < 0) cur = 0;
    }
    printf("%lld\n", best);
    return 0;
}
