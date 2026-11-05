#include <stdio.h>
#include <stdlib.h>

int main()
{
    int n, i, j, t, m;
    int *a;
    scanf("%d", &n);
    if (n <= 0) return 1;
    a = (int *)malloc(n * sizeof(int));
    for (i = 0; i < n; i++) scanf("%d", &a[i]);
    for (i = 0; i < n - 1; i++) {
        m = i;
        for (j = i + 1; j < n; j++)
            if (a[j] < a[m]) m = j;
        t = a[i]; a[i] = a[m]; a[m] = t;
    }
    for (i = 0; i < n; i++) printf("%d ", a[i]);
    printf("\n");
    return 0;
}
