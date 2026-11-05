#include <stdio.h>

int a[100000];

int main()
{
    int n, i, best, cur;
    scanf("%d", &n);
    for (i = 0; i < n; i++) scanf("%d", &a[i]);
    best = a[0]; cur = a[0];
    for (i = 1; i < n; i++) {
        cur = cur + a[i] > a[i] ? cur + a[i] : a[i];   /* bug: int overflows past 2^31 */
        if (cur > best) best = cur;
    }
    printf("%d\n", best);
    return 0;
}
